# These imports are needed to access the parsing functions (which you're likely to use in parameter parsing),
# the Campaign data object and the host parent class.
from core.parsing import isPositiveInt
from core.campaign import Campaign
from core.host import host, countedConnectionObject
import core.execution

import threading
import errno
import stat
import subprocess
import os
import struct
import time
import socket

#==========================
# Multiplexing connections
#
# This DAS4 host implementation will multiplex connections to the nodes over a single mux channel. This channel uses the following
# protocol. Each message starts with 1 byte, the opcode. Depending on the opcode the rest of the message is interpreted.
#
# Muxer sends:
# - +
#    Setup a new connection. Bytes 1..4 contain the connection number of the new connection, bytes 5..8 the number of bytes in the
#    address of the node, bytes 9..12 the number of bytes in the command to run. The rest of the message contains the specified
#    numbers of bytes of hostname followed by the specified number of bytes of command.
#
# - -
#    Remove a connection. Bytes 1..4 contain the connection number of the connection to be removed. Note the exact length of 5
#    bytes of the message.
#
# - 0
#    A \n terminated message. Bytes 1..4 contain the connection number. The rest of the message, up to and including the \n contains
#    the message to be passed on over that connection. Note the minimum length of 6 bytes before reading the contents. The message
#    may not include \n.
#
# - 1
#    A non-terminated message. Bytes 1..4 contain the connection number. Bytes 5..8 contain the length of the message. Note the
#    minimum length of 9 bytes before reading any data, and the exact length being 9 + the message length. The message may include
#    \n.
#
# - X
#    Tells the demuxer to quit. No further operation can be expected.
#
# - \n
#    NOP
#
# Demuxer sends:
# - +
#    Response to + or = from muxer. Followed by a single + for succes. Followed by - for failure. In the last case four bytes of
#    error message length follow, followed by that many bytes error message.
#
# - -
#    Response to - from muxer, or own signal that no more data will arrive for this connection. Followed by four bytes of connection
#    number. Signals connection has been closed, either succesfully or unsuccesfully. No data for the closed connection will follow.
#
# - 0
#    A \n terminated message. Bytes 1..4 contain the connection number. The rest of the message, up to and including the \n contains
#    the message to be passed on over that connection. Note the minimum length of 6 bytes before reading the contents. The message
#    may not include \n.
#
# - 1
#    A non-terminated message. Bytes 1..4 contain the connection number. Bytes 5..8 contain the length of the message. Note the
#    minimum length of 9 bytes before reading any data, and the exact length being 9 + the message length. The message may include
#    \n.
#
# - X
#    Tells the muxer the demuxer has quitted. Followed by four bytes of error message length, followed by that many bytes of error
#    message. No further operation can be expected.
#
#==========================

# ==== Paramiko is used for the SSH connections ====
paramiko = None
try:
    paramiko = __import__('paramiko', globals(), locals() )
except ImportError:
    raise Exception( "The host:das4 module requires the paramiko package to be available. Please make sure it's available." )
# ==== /Paramiko ====

# ==== parseError parsing helper function ====
def parseError( msg ):
    """
    A simple helper function to make parsing a lot of parameters a bit nicer.
    """
    raise Exception( "Parse error for host object on line {0}: {1}".format( Campaign.currentLineNumber, msg ) )
# ==== /parseError ====

# ==== getIPAddresses() generator function, returns IP adresses of this host ====
# Below several implementations are tested for availability and the best is chosen
have_windll = False
have_ifconfig = None
try:
    libs = ['windll', 'Structure', 'sizeof', 'POINTER', 'byref', 'c_ulong', 'c_uint', 'c_ubyte', 'c_char']
    ctypes = __import__('ctypes', globals(), locals(), libs)
    for lib in libs:
        if not hasattr(ctypes, lib):
            raise ImportError()
    have_windll = True
except ImportError:
    if os.path.exists( '/sbin/ifconfig' ):
        have_ifconfig = '/sbin/ifconfig'
    elif os.path.exists( '/usr/sbin/ifconfig' ):
        have_ifconfig = '/usr/sbin/ifconfig'
    else:
        out, _ = subprocess.Popen( 'which ifconfig', stdout = subprocess.PIPE, shell = True ).communicate()
        if out is None or out == '' or not os.path.exists( 'out' ):
            Campaign.logger.log( "Warning: host:das4 may need to try and find the network you're in. You don't seem to be on windows and ifconfig can't be found either; falling back to contacting gmail.com to get a local IP. Please specify your headNode to prevent problems from this." )
        else:
            have_ifconfig = out

if have_windll:
    # Windows based method using windll magic
    # Thanks for this method goes to DzinX in http://stackoverflow.com/questions/166506/finding-local-ip-addresses-using-pythons-stdlib
    def getIPAddresses():
        from ctypes import Structure, windll, sizeof
        from ctypes import POINTER, byref
        from ctypes import c_ulong, c_uint, c_ubyte, c_char
        MAX_ADAPTER_DESCRIPTION_LENGTH = 128
        MAX_ADAPTER_NAME_LENGTH = 256
        MAX_ADAPTER_ADDRESS_LENGTH = 8
        class IP_ADDR_STRING(Structure):
            pass
        LP_IP_ADDR_STRING = POINTER(IP_ADDR_STRING)
        IP_ADDR_STRING._fields_ = [
            ("next", LP_IP_ADDR_STRING),
            ("ipAddress", c_char * 16),
            ("ipMask", c_char * 16),
            ("context", c_ulong)]
        class IP_ADAPTER_INFO (Structure):
            pass
        LP_IP_ADAPTER_INFO = POINTER(IP_ADAPTER_INFO)
        IP_ADAPTER_INFO._fields_ = [
            ("next", LP_IP_ADAPTER_INFO),
            ("comboIndex", c_ulong),
            ("adapterName", c_char * (MAX_ADAPTER_NAME_LENGTH + 4)),
            ("description", c_char * (MAX_ADAPTER_DESCRIPTION_LENGTH + 4)),
            ("addressLength", c_uint),
            ("address", c_ubyte * MAX_ADAPTER_ADDRESS_LENGTH),
            ("index", c_ulong),
            ("type", c_uint),
            ("dhcpEnabled", c_uint),
            ("currentIpAddress", LP_IP_ADDR_STRING),
            ("ipAddressList", IP_ADDR_STRING),
            ("gatewayList", IP_ADDR_STRING),
            ("dhcpServer", IP_ADDR_STRING),
            ("haveWins", c_uint),
            ("primaryWinsServer", IP_ADDR_STRING),
            ("secondaryWinsServer", IP_ADDR_STRING),
            ("leaseObtained", c_ulong),
            ("leaseExpires", c_ulong)]
        GetAdaptersInfo = windll.iphlpapi.GetAdaptersInfo
        GetAdaptersInfo.restype = c_ulong
        GetAdaptersInfo.argtypes = [LP_IP_ADAPTER_INFO, POINTER(c_ulong)]
        adapterList = (IP_ADAPTER_INFO * 10)()
        buflen = c_ulong(sizeof(adapterList))
        rc = GetAdaptersInfo(byref(adapterList[0]), byref(buflen))
        if rc == 0:
            for a in adapterList:
                adNode = a.ipAddressList
                while True:
                    # Added per comment on the original code. Not tested:
                    if not hasattr(adNode, 'ipAddress'):
                        adNode = adNode.content
                    # /Added
                    ipAddr = adNode.ipAddress
                    # Added check for 127.0.0.1
                    if ipAddr and ipAddr != '127.0.0.1':
                        yield ipAddr
                    adNode = adNode.next
                    if not adNode:
                        break
elif have_ifconfig:
    # *nix based method based on reading the output of ifconfig
    def getIPAddresses():
        import re
        co = subprocess.Popen([have_ifconfig], stdout = subprocess.PIPE)
        ifconfig = co.stdout.read()
        del co
        ip_regex = re.compile('((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-4]|2[0-5][0-9]|[01]?[0-9][0-9]?)).*')
        ips = [match[0] for match in ip_regex.findall(ifconfig, re.MULTILINE) if not match[0] == '127.0.0.1']
        for ip in ips:
            yield ip
else:
    # Unreliable platform-independent method (requires the ability to connect to gmail.com:80)
    def getIPAddresses():
        s = socket.socket( socket.AF_INET, socket.SOCK_DGRAM )
        s.connect( ( 'gmail.com', 80 ) )
        ip = s.getsockname()[0]
        s.close()
        yield ip
# ==== /getIPAddresses() ====     

# ==== getHostnameByIP(ip) Returns the hostname of the given IP ====
def getHostnameByIP(ip):
    socket.setdefaulttimeout(10)
    return socket.gethostbyaddr(ip)[0]
# ==== /getHostnameByIP(ip)

class stringbuffer():
    buf = ''
    
    def __init__(self):
        self.buf = ''
    
    def write(self, data):
        self.buf += data
    
    def read(self, len_ = None):
        if len_ == None:
            res = self.buf
            self.buf = ''
            return res
        actualLen = len_
        if actualLen > len(self.buf):
            actualLen = len(self.buf)
        res = self.buf[:actualLen]
        self.buf = self.buf[actualLen:]
        return res
    
    def readline(self):
        pos = self.buf.find('\n')
        if pos == -1:
            return self.read()
        else:
            return self.read(pos+1)

def keepAlive(muxIO, muxIO__lock, host_, timerlist, timerindex):
    if host_.isInCleanup():
        return
    try:
        muxIO__lock[0].acquire()
        muxIO[0].write('\n')
    finally:
        muxIO__lock[0].release()
    if host_.isInCleanup():
        return
    oldtimer = timerlist[timerindex]
    timerlist[timerindex] = threading.Timer(30.0, keepAlive, args = [muxIO, muxIO__lock, host_, timerlist, timerindex])
    timerlist[timerindex].start()
    del oldtimer

class das4MuxConnectionObject(countedConnectionObject):
    """
    SSH connection object for multiplexed paramiko connections
    """
    
    muxIO = None
    muxIO__lock = None
    connNumber = None
    
    inputBuffer = None
    inputBuffer__lock = None
    noMoreInput = False
    
    client = None
    sftpChannel = None
    sftp__lock = None
    sftpScriptname = None
    sftpConnectionList = None
    sftpChannelCreated = False
    
    def __init__(self, connNumber, muxIO, muxIO__lock, client, sftpScriptName, sftpConnectionList):
        countedConnectionObject.__init__(self)
        self.muxIO = muxIO
        self.connNumber = struct.pack( '!I', connNumber )
        self.muxIO__lock = muxIO__lock
        self.inputBuffer = stringbuffer()
        self.inputBuffer__lock = threading.Lock()
        self.client = client
        self.sftp__lock = threading.Lock()
        self.sftpScriptname = sftpScriptName
        self.noMoreInput = False
        self.sftpConnectionList = sftpConnectionList
        self.sftpChannelCreated = False
   
    def close(self):
        countedConnectionObject.close(self)
        num = struct.unpack( '!I', self.connNumber )[0]
        alreadyClosed = False
        try:
            self.muxIO__lock[0].acquire()
            self.muxIO[0].write( '-{0}'.format( self.connNumber ) )
            self.muxIO[0].flush()
        except socket.error as e:
            if e.args != 'Socket is closed':
                raise
            alreadyClosed = True
        finally:
            self.muxIO__lock[0].release()
        if not alreadyClosed:
            try:
                self.muxIO__lock[1].acquire()
                try:
                    das4MuxConnectionObject.readmux(self.muxIO, self.muxIO__lock, '-{0}'.format( self.connNumber ) )
                finally:
                    if num in self.muxIO[2]:
                        del self.muxIO[2][num]
            except Exception as e:
                Campaign.logger.log( "Ignored exception while removing connection {0} from the list of mux connections: {1}".format( self.getIdentification(), e ) )
                Campaign.logger.exceptionTraceback()
            finally:
                self.muxIO__lock[1].release()
        try:
            self.sftp__lock.acquire()
            if self.sftpChannel:
                self.sftpChannel.close()
                Campaign.debuglogger.log( self.getIdentification(), 'SFTP CHANNEL REMOVED DURING CLOSE' )
                del self.sftpChannel
                self.sftpChannel = None
                self.sftpConnectionList[1] = None
        except socket.error as e:
            if e.args != 'Socket is closed':
                raise
            del self.sftpChannel
            self.sftpChannel = None
            self.sftpConnectionList[1] = None
        finally:
            self.sftp__lock.release()
            del self.sftpConnectionList
            self.sftpConnectionList = None
            del self.client
            self.client = None
            del self.muxIO
            self.muxIO = None
            del self.muxIO__lock
            self.muxIO__lock = None
            del self.inputBuffer
            self.inputBuffer = None
            del self.inputBuffer__lock
            self.inputBuffer__lock = None
            Campaign.debuglogger.closeChannel( self.getIdentification() )
    
    def write(self, msg):
        multi = False
        pos = msg.find( '\n' )
        if pos != len(msg) - 1:
            multi = True
        try:
            self.muxIO__lock[0].acquire()
            if multi:
                self.muxIO[0].write( '1{0}{1}{2}'.format( self.connNumber, struct.pack( '!I', len(msg) ), msg ) )
            else:
                self.muxIO[0].write( '0{0}{1}'.format( self.connNumber, msg ) )
            self.muxIO[0].flush()
        finally:
            self.muxIO__lock[0].release()

    def flush(self):
        pass
            
    @staticmethod
    def readmux(muxIO, muxIO__lock, expect):
        """
        Reads data from the mux channel and processes it as necessary.
        
        Data is written into the correct connection's inputBuffer and any other packet is a failure if not expected.
        
        The expect parameter tells the function what data to expect and, hence, finish on. The value of expect may be:
            - '+' for expecting response to a '+' message, the function will return on succesfull connection setup and raise an error on received failure
            - '-NNNN' for expecting response to a '-' message for connection with packed number NNNN, the function will return when
                the response has been received
            - '0NNNN' with NNNN being the packed connection number of the connection data is expected from; the function will
                return when a \n has been written to the inputbuffer of the connection data is expected for, or no more data will
                arrive for that connection
            - '1NNNNLLLL' with NNN being the packed connection number of the connection data is expected from and LLLL being the
                packed number of bytes being expected; the function will return when the number of bytes has been written to the
                inputbuffer of the connection data is expected for, or no more data will arrive for that connection
            - '1NNNN' with NNN being the packed connection number of the connection data is expected from; the function will return
                when no more data will arrive for that connection
        
        
        Note that the expect parameter is not checked for validity: incorrect values will lead to an infinite loop with hopefully an
        Exception being raised in the near future.
        
        @param    muxIO          The muxIO tuple containing (mux write stream, mux read stream, mux connection map)
        @param    muxIO__lock    The lock for the muxIO tuple; this will be acquired by the method
        @param    expect         The expected message header, either '+', '-' or '0NNNN' with NNNN being a packed connection number
        """
        expectLen = None
        if expect[0] == 1 and len(expect) == 9:
            expectLen = struct.unpack( '!I', expect[5:] )[0]
        try:
            muxIO__lock[1].acquire()
            while True:
                opcode = muxIO[1].read(1)
                if opcode == '':
                    raise Exception( "Unexpected EOF on mux channel; expected 1 byte opcode, got ''" )
                elif opcode == 'X':
                    # Muxer quit, failure
                    buf = muxIO[1].read(4)
                    if len(buf) < 4:
                        raise Exception( "Remote demuxer suddenly quit, followed by unexpected EOF on mux channel; expected 4 bytes error message length, got {0} bytes".format( len( buf ) ) )
                    errlen = struct.unpack( '!I', buf )[0]
                    problem = muxIO[1].read(errlen)
                    if len(problem) < errlen:
                        raise Exception( "Remote demuxer suddenly quit, followed by unexpected EOF on mux channel; expected {0} bytes of error message, got {1} bytes: '{2}'".format( errlen, len(problem), problem ) )
                    raise Exception( "Remote demuxer suddenly quit. Reported problem: {0}".format( problem ) )
                elif opcode == '+':
                    # Response to a '+' message: new connection. Fail if unexpected
                    if expect != '+':
                        raise Exception( "A connection was apparently opened, but I was just reading data. Insanity." )
                    # Read the result
                    result = muxIO[1].read(1)
                    if result == '+':
                        # Succesful connection setup, we're done
                        return
                    elif result == '-':
                        # Failed connection setup, read error message and raise exception
                        buf = muxIO[1].read(4)
                        if len(buf) < 4:
                            raise Exception( "The connection could not be set up over the mux channel, followed by unexpected EOF on mux channel; expected 4 bytes error message length, got {0} bytes".format( len( buf ) ) )
                        errlen = struct.unpack( '!I', buf )[0]
                        problem = muxIO[1].read(errlen)
                        if len(problem) < errlen:
                            raise Exception( "The connection could not be set up over the mux channel, followed by unexpected EOF on mux channel; expected {0} bytes of error message, got {1} bytes: '{2}'".format( errlen, len(problem), problem ) )
                        raise Exception( "The connection could not be set up over the mux channel. Reported problem: {0}".format( problem ) )
                    elif result == '':
                        raise Exception( "Unexpected EOF on mux channel; expected 1 byte new connection result, got ''" )
                    else:
                        raise Exception( "Connection setup over mux channel went awry: incorrect result {0}".format( result ) )
                elif opcode == '-':
                    # Response to a '-' message: close connection. Done if expected
                    connbuf = muxIO[1].read(4)
                    if len(connbuf) < 4:
                        raise Exception( "Unexpected EOF on mux channel; expected 4 bytes connection number, got {0} bytes".format( len( connbuf ) ) )
                    connNumber = struct.unpack( '!I', connbuf )[0]
                    if connNumber in muxIO[2]:
                        muxIO[2][connNumber].noMoreInput = True
                        if expect == '0{0}'.format( connbuf ):
                            return
                        if expect[:5] == '1{0}'.format( connbuf ):
                            return
                    if expect == '-{0}'.format( connbuf ):
                        return
                elif opcode == '0' or opcode == '1':
                    # Data for a connection: read the connection number
                    connbuf = muxIO[1].read(4)
                    if len(connbuf) < 4:
                        raise Exception( "Unexpected EOF on mux channel; expected 4 bytes connection number, got {0} bytes".format( len( connbuf ) ) )
                    if opcode == '0':
                        # Opcode '0': a single line of data, read that
                        data = muxIO[1].readline()
                        if data == '' or data[-1] != '\n':
                            raise Exception( "Unexpected EOF on mux channel; expected a single line, got '{0}'".format( data ) )
                        datalen = len(data)
                    else:
                        # Opcode '1': a number of characters of data, read them
                        buf = muxIO[1].read(4)
                        if len(buf) != 4:
                            raise Exception( "Unexpected EOF on mux channel; expected 4 bytes length, got {0} bytes".format( len( buf ) ) )
                        datalen = struct.unpack( '!I', buf )[0]
                        data = muxIO[1].read(datalen)
                        if len(data) != datalen:
                            raise Exception( "Unexpected EOF on mux channel; expected {0} bytes of data, got {1} bytes: '{2}'".format( datalen, len(data), data ) )
                    # A connection's data. Write into that connection's buffer.
                    incomingConnNumber = struct.unpack( '!I', connbuf )[0]
                    if incomingConnNumber not in muxIO[2]:
                        raise Exception( "Received data on mux channel for unknown mux connection {0}. Data: {1}".format( incomingConnNumber, data ) )
                    try:
                        muxIO[2][incomingConnNumber].inputBuffer__lock.acquire()
                        muxIO[2][incomingConnNumber].inputBuffer.write( data )
                        dataLen = len(muxIO[2][incomingConnNumber].inputBuffer.buf)
                    finally:
                        muxIO[2][incomingConnNumber].inputBuffer__lock.release()
                    # Return if data was expected for this connection and a \n has been found
                    if expect[0] == '0' and connbuf == expect[1:] and data.find( '\n' ) > -1:
                        return
                    if expect[0] == '1' and dataLen >= expectLen:
                        return
                else:
                    raise Exception( "Unexpected opcode over mux channel: {0}".format( opcode ) )
        finally:
            muxIO__lock[1].release()
    
    def readline(self):
        try:
            self.inputBuffer__lock.acquire()
            if len(self.inputBuffer.buf) > 0:
                if self.inputBuffer.buf.find('\n') > -1:
                    return self.inputBuffer.readline()
                elif self.noMoreInput:
                    return self.inputBuffer.read()
            elif self.noMoreInput:
                return ''
        finally:
            self.inputBuffer__lock.release()
        # No data available, we need to start reading the mux channel and return when we have data
        haveLock = False
        try:
            while not self.muxIO__lock[1].acquire(False):
                try:
                    self.inputBuffer__lock.acquire()
                    if len(self.inputBuffer.buf) > 0:
                        if self.inputBuffer.buf.find('\n') > -1:
                            return self.inputBuffer.readline()
                        elif self.noMoreInput:
                            return self.inputBuffer.read()
                    elif self.noMoreInput:
                        return ''
                finally:
                    self.inputBuffer__lock.release()
                time.sleep(0.05)
            haveLock = True
            try:
                # Check again, just to be sure
                self.inputBuffer__lock.acquire()
                if len(self.inputBuffer.buf) > 0:
                    if self.inputBuffer.buf.find('\n') > -1:
                        return self.inputBuffer.readline()
                    elif self.noMoreInput:
                        return self.inputBuffer.read()
                elif self.noMoreInput:
                    return ''
            finally:
                self.inputBuffer__lock.release()
            # Do some reading
            das4MuxConnectionObject.readmux(self.muxIO, self.muxIO__lock, '0{0}'.format( self.connNumber ))
            try:
                # Now it's there
                self.inputBuffer__lock.acquire()
                return self.inputBuffer.readline()
            finally:
                self.inputBuffer__lock.release()
        finally:
            if haveLock:
                self.muxIO__lock[1].release()
    
    def read(self, len_ = None):
        if len_ is None:
            try:
                self.inputBuffer__lock.acquire()
                if self.noMoreInput:
                    return self.inputBuffer.read()
            finally:
                self.inputBuffer__lock.release()
            haveLock = False
            try:
                while not self.muxIO__lock[1].acquire(False):
                    try:
                        self.inputBuffer__lock.acquire()
                        if self.noMoreInput:
                            return self.inputBuffer.read()
                    finally:
                        self.inputBuffer__lock.release()
                    time.sleep(0.05)
                haveLock = True
                try:
                    # Check again, just to be sure
                    self.inputBuffer__lock.acquire()
                    if self.noMoreInput:
                        return self.inputBuffer.read()
                finally:
                    self.inputBuffer__lock.release()
                # Do some reading
                das4MuxConnectionObject.readmux(self.muxIO, self.muxIO__lock, '1{0}'.format( self.connNumber ))
                try:
                    # Now it's there
                    self.inputBuffer__lock.acquire()
                    return self.inputBuffer.read()
                finally:
                    self.inputBuffer__lock.release()
            finally:
                if haveLock:
                    self.muxIO__lock[1].release()
        else:
            try:
                self.inputBuffer__lock.acquire()
                if len(self.inputBuffer.buf) >= len_:
                    return self.inputBuffer.read(len_)
                elif self.noMoreInput:
                    return self.inputBuffer.read()
            finally:
                self.inputBuffer__lock.release()
            # No data available, we need to start reading the mux channel and return when we have data
            haveLock = False
            try:
                while not self.muxIO__lock[1].acquire(False):
                    try:
                        self.inputBuffer__lock.acquire()
                        if len(self.inputBuffer.buf) >= len_:
                            return self.inputBuffer.read(len_)
                        elif self.noMoreInput:
                            return self.inputBuffer.read()
                    finally:
                        self.inputBuffer__lock.release()
                    time.sleep(0.05)
                haveLock = True
                try:
                    # Check again, just to be sure
                    self.inputBuffer__lock.acquire()
                    if len(self.inputBuffer.buf) >= len_:
                        return self.inputBuffer.read(len_)
                    elif self.noMoreInput:
                        return self.inputBuffer.read()
                finally:
                    self.inputBuffer__lock.release()
                # Do some reading
                das4MuxConnectionObject.readmux(self.muxIO, self.muxIO__lock, '1{0}{1}'.format( self.connNumber, struct.pack( '!I', len_ ) ))
                try:
                    # Now it's there
                    self.inputBuffer__lock.acquire()
                    return self.inputBuffer.read(len_)
                finally:
                    self.inputBuffer__lock.release()
            finally:
                if haveLock:
                    self.muxIO__lock[1].release()
    
    def createSFTPChannel(self):
        if self.isClosed():
            raise Exception( "Can't create an SFTP channel for a closed SSH connection on connection {0}".format( self.getIdentification( ) ) )
        try:
            self.sftp__lock.acquire()
            if self.sftpChannel:
                return True
            # Slightly more elaborate than client.open_sftp(), since we need special handling
            if len(self.sftpConnectionList) > 1 and self.sftpConnectionList[1] is not None:
                self.sftpChannel = self.sftpConnectionList[1]
                return True
            self.sftpChannelCreated = True
            t = self.sftpConnectionList[0].get_transport()
            chan = t.open_session()
            chan.exec_command(self.sftpScriptname)
            self.sftpChannel = paramiko.SFTPClient(chan)
            self.sftpConnectionList.append( self.sftpChannel )
            Campaign.debuglogger.log( self.getIdentification(), 'SFTP CHANNEL CREATED' )
        finally:
            self.sftp__lock.release()
        return False
    
    def removeSFTPChannel(self):
        if self.isClosed():
            return
        try:
            self.sftp__lock.acquire()
            if not self.sftpChannel or not self.sftpChannelCreated:
                return
            self.sftpChannel.close()
            del self.sftpChannel
            self.sftpChannel = None
            self.sftpConnectionList[1] = None
            Campaign.debuglogger.log( self.getIdentification(), 'SFTP CHANNEL REMOVED' )
        finally:
            self.sftp__lock.release()
    
    @staticmethod
    def existsRemote(sftp, remotePath):
        found = True
        try:
            sftp.stat( remotePath )
        except IOError as e:
            found = False
            if not e.errno == errno.ENOENT:
                raise e
        return found

    @staticmethod
    def isRemoteDir(sftp, remotePath):
        attribs = sftp.stat(remotePath)
        return stat.S_ISDIR( attribs.st_mode )

class das4ConnectionObject(countedConnectionObject):
    """
    SSH connection object for paramiko connections
    
    This connection object is a small adaptation from the one in host:ssh.
    """
    
    client = None
    io = None
    
    sftpChannel = None
    sftp__lock = None
    sftpScriptname = None
    
    def __init__(self, client, io, sftpScriptName ):
        countedConnectionObject.__init__(self)
        self.client = client
        self.sftp__lock = threading.Lock()
        self.io = io
        self.sftpScriptname = sftpScriptName
    
    def close(self):
        countedConnectionObject.close(self)
        try:
            self.sftp__lock.acquire()
            if self.sftpChannel:
                self.sftpChannel.close()
                Campaign.debuglogger.log( self.getIdentification(), 'SFTP CHANNEL REMOVED DURING CLOSE' )
                del self.sftpChannel
                self.sftpChannel = None
        finally:
            self.sftp__lock.release()
            self.client.close()
            del self.client
            self.client = None
            Campaign.debuglogger.closeChannel( self.getIdentification() )
    
    def write(self, msg):
        self.io[0].write( msg )
        self.io[0].flush()
    
    def readline(self):
        line = self.io[1].readline()
        return line
    
    def createSFTPChannel(self):
        if self.isClosed():
            raise Exception( "Can't create an SFTP channel for a closed SSH connection on connection {0}".format( self.getIdentification( ) ) )
        try:
            self.sftp__lock.acquire()
            if self.sftpChannel:
                return True
            # Slightly more elaborate than client.open_sftp(), since we need special handling
            t = self.client.get_transport()
            chan = t.open_session()
            chan.exec_command(self.sftpScriptname)
            self.sftpChannel = paramiko.SFTPClient(chan)
            Campaign.debuglogger.log( self.getIdentification(), 'SFTP CHANNEL CREATED' )
        finally:
            self.sftp__lock.release()
        return False
    
    def removeSFTPChannel(self):
        if self.isClosed():
            return
        try:
            self.sftp__lock.acquire()
            if not self.sftpChannel:
                return
            self.sftpChannel.close()
            del self.sftpChannel
            self.sftpChannel = None
            Campaign.debuglogger.log( self.getIdentification(), 'SFTP CHANNEL REMOVED' )
        finally:
            self.sftp__lock.release()
    
    @staticmethod
    def existsRemote(sftp, remotePath):
        found = True
        try:
            sftp.stat( remotePath )
        except IOError as e:
            found = False
            if not e.errno == errno.ENOENT:
                raise e
        return found

    @staticmethod
    def isRemoteDir(sftp, remotePath):
        attribs = sftp.stat(remotePath)
        return stat.S_ISDIR( attribs.st_mode )

class das4(host):
    """
    DAS4 host implementation.
    
    Extra parameters:
    - headnode              The hostname of the DAS4 host to SSH to initially. Optional, if left out the module
                            will try and determine which network you're on and use the local entry node. If
                            you're not on one of the networks of the institutes hosting DAS4 this will give an
                            error.
                            
                            The automated lookup test uses dig -x for a reverse lookup on any inet address found
                            in ifconfig, except 127.0.0.1, and tries to see if it matches any of the following
                            networks:
                                network        headnode
                                -------        --------
                                .vu.nl         fs0.das4.vu.nl
                                .liacs.nl      fs1.das4.liacs.nl
                                .uva.nl        fs4.das4.science.uva.nl
                                .tudelft.nl    fs3.das4.tudelft.nl
                                .astron.nl     fs5.das4.astron.nl
                            Note that fs2.das4.science.uva.nl won't be used automatically.
                            
                            The above table also holds all valid values for this parameter, unless the
                            headNodeOverride parameters is set.
    - nNodes                The number of nodes to request, a positive integer. Optional, defaults to 2.
    - reserveTime           A positive number of seconds to reserve the nodes; note that you should take into 
                            account that the nodes need to be reserved during setup, so some setup steps can
                            still occur between reservation and the actual running of the test scenarios. It
                            is recommended to reserve the nodes for a few minutes more than the maximum
                            execution time of the scenario. Optional, defaults to 900.
    - user                  The username to use for logging in on the DAS4 system. Required.
    - headnodeOverride      Set to anything but "" to override the validity checks on the headNode parameter.
                            Use this for custom headNodes or to bypass DNS lookups by providing the IP of the
                            headnode. Optional.
    
    Requirements of the DAS4 module:
    - You must be able to use SSH from the specified (or selected) headnode to the other nodes without interaction
        (i.e. passwordless)
    - It is HIGHLY RECOMMENDED to place SGE_KEEP_TMPFILES="no" in ~/.bashrc on your headnode: the test framework
        will try and cleanup nicely,  but will first and foremost honor the reservation system. This means that if
        an execution takes too long with regard to the reservation time, for example due to a longer setup than
        anticipated, the reserved nodes can't be cleaned by the test framework. Specifically files in /local will
        not be removed in that case. Setting SGE_KEEP_TMPFILES="no" prevents this.
    
    It is important to understand that while the DAS4 is well equipped to commandeer your nodes in parallel, this
    module will actually split itself into one host per node. This means that, after setup, you will end up with
    each node as a separate host and they will hence be used as single hosts.
    
    For the reservation the requests from all DAS4 host objects will be taken together and put into one big
    request. The advantage of this approach is that it makes sure that all hosts are available at the same time.
    For this reason it is also NOT supported to have multiple DAS4 host objects with different headnodes in the
    same scenario. In other words: whenever you have multiple DAS4 host objects in the same scenario, make sure
    they have the same headnode configured.
    
    Traffic control on the DAS4 is currently not supported. If you try and use it, anyway, results are
    unpredictable. Most likely it will break the moment any DAS4 node needs to fall back to full IP-range based
    traffic control; thereby also breaking it for other users. For your convenience and experimentation no
    warnings or errors will pop up if you try, though.  
    """

    headNode = None                         # Address of the headNode to use
    nNodes = None                           # Number of nodes to reserve
    reserveTime = None                      # Number of seconds to reserve the nodes
    user = None                             # Username to use as login name on the DAS4
    headNode_override = False               # Set to True to disable headNode validity checks
    
    masterConnection = None                 # The master connection to the headnode; all slave hosts will connect through port
                                            # forwards over this connection.
    masterIO = []                           # Will be an array of length 2 with the input and output streams for masterConnection
    muxIO = []                              # Will be an array of length 3 with the input and output streams for the mux channel,
                                            # and a map of existing mux channels
    muxIO__lock = (threading.RLock(),threading.RLock)
                                            # Locks for muxIO (write_lock, read_lock)
    # @static
    muxConnCount = 0                        # Number of created mux connections
    # @static
    muxConnCount__lock = threading.Lock()   # Lock for number of mux connections
    sftpConnections = {}                    # Map from node name to [client, channel] for SFTP (or [client] is the channel has not
                                            # been made yet)
    keepAliveTimers = []                    # List of timers that run the keepalive function
    secondaryMuxIO = {}                     # Map of secondary mux channel streams and channels, which are basically mux channels
                                            # over the primary muxIO mux channel [(write_stream, read_stream, channelmap),...].
                                            # Mapped from hostname.
    secondaryMuxIO__lock = {}               # Locks the secondaryMuxIO [(write_lock, read_lock),...]. Mapped from hostname.
    
    tempPersistentDirectory = None          # String with the temporary persistent directory on the headnode
    reservationID = None                    # Reservation identifier
    nodeSet = []                            # List of node names to be used by this master host, nodeSet[0] is the node name of this slave host
    slaves = []                             # list of slaves of this master node, None for non-master nodes
    bogusRemoteDir = False                  # Flag to signal whether to ignore the value in self.remoteDirectory
    
    # DAS4 host objects come in three types:
    # - supervisor
    # - master
    # - slave
    #
    # Each master is also a slave, the supervisor is also a master (and hence also a slave).
    # The master hosts are the ones declared in the scenario files. Each host object from the scenario files of
    # type das4 will get one host object that will become a master host. The master hosts have their headNode,
    # nNodes and reserveTime parameters set, indicating which headNode they wish to use and how many nodes to
    # request. The supervisor host is the first master host that starts preparation and supervises the master
    # connection and the reservation of the nodes. The slave hosts are the final connections to the nodes
    # themselves, one slave host for each node. The slave hosts are mostly created by the master hosts. 
    #
    # The supervisor node will first take together all requests from DAS4 hosts and pass them as one big request
    # to the headnode. It will retrieve the reservationID from that and will wait for the nodes to become
    # available. The nodes are then divided over the master hosts in their nodeSet. The master hosts then make
    # the slave hosts for each node they have (except the first which they serve themselves).
    #
    # The difference between the hosts can be detected as follows:
    #    host type                   nNodes          reservationID       len(nodeSet)    simple test
    #    first unprepared master     >= 1            None                None            if not self.nodeSet:
    #    supervisor                  >= 1            string              nNodes          if self.reservationID:
    #    master                      >= 1            None                nNodes          if self.nNodes:
    #    slave                       None            None                1               if not self.nNodes:
    # Note the special host type 'first unprepared master', here. This is the master host that will be upgraded
    # to supervisor. The simple test gives the simplest test that should work.

    def __init__(self, scenario):
        """
        Initialization of a generic host object.
        
        @param  scenario        The ScenarioRunner object this host object is part of.
        """
        host.__init__(self, scenario)
        self.masterIO = None
        self.muxIO = None
        self.muxIO__lock = (threading.RLock(), threading.RLock())
        self.nodeSet = None
        self.slaves = None
        self.sftpConnections = {}
        self.keepAliveTimers = []
        self.secondaryMuxIO = {}
        self.secondaryMuxIO__lock = {}

    def parseSetting(self, key, value):
        """
        Parse a single setting for this object.

        Settings are written in text files in a key=value fashion.
        For each such setting that belongs to this object this method will be called.

        After all settings have been given, the method checkSettings will be called.

        If a setting does not parse correctly, this method raises an Exception with a descriptive message.

        Subclassers should first parse their own settings and then call this implementation to have the
        generic settings parsed and to have any unknown settings raise an Exception.
        
        @param  key     The name of the parameter, i.e. the key from the key=value pair.
        @param  value   The value of the parameter, i.e. the value from the key=value pair.
        """
        if key == 'headNode' or key == 'headnode':
            if key == 'headNode':
                Campaign.logger.log( "headNode is a strange camelcase. It has been deprecated in favor of headnode.")
            if self.headNode:
                parseError( "headNode already set: {0}".format( self.headNode ) )
            self.headNode = value
        elif key == 'headNodeOverride' or key == 'headnodeOverride':
            if key == 'headNodeOverride':
                Campaign.logger.log( "headNodeOverride is a strange camelcase. It has been deprecated in favor of headnodeOverride.")
            if value != '':
                self.headNode_override = True
        elif key == 'nNodes':
            if self.nNodes:
                parseError( "Number of nodes already set: {0}".format( self.nNodes ) )
            if not isPositiveInt( value, True ):
                parseError( "Number of nodes should be a positive, non-zero integer" )
            self.nNodes = int(value)
        elif key == 'reserveTime':
            if self.reserveTime:
                parseError( "Reserve time already set: {0}".format( self.reserveTime ) )
            if not isPositiveInt( value, True ):
                parseError( "Reserve time should be a positive, non-zero integer number of seconds" )
            self.reserveTime = int(value)
        elif key == 'user':
            if self.user:
                parseError( "User already set: {0}".format( self.user ) )
            self.user = value
        else:
            host.parseSetting(self, key, value)

    def checkSettings(self):
        """
        Check the sanity of the settings in this object.
empty
        This method is called after all calls to parseSetting(...) have been done.
        Any defaults may be set here as well.

        An Exception is raised in the case of insanity.
        """
        if self.name == '':
            if 'das4' in self.scenario.getObjectsDict( 'host' ):
                raise Exception( "Name not set for host object declared at line {0}, but the default name (das4) was already taken.".format( self.declarationLine ) )
            else:
                self.name = 'das4'
        host.checkSettings(self)
        if not self.user:
            raise Exception( "The user parameter is not optional for host {0}.".format( self.name ) )
        if not self.reserveTime:
            self.reserveTime = 900
        if not self.nNodes:
            self.nNodes = 2
        if not self.headNode_override and self.headNode:
            if self.headNode not in [ 'fs0.das4.vu.nl', 'fs1.das4.liacs.nl', 'fs4.das4.science.uva.nl', 'fs3.das4.tudelft.nl', 'fs5.das4.astron.nl', 'fs2.das4.science.uva.nl' ]:
                raise Exception( "The host {1} was given {0} as headnode, but that is not a headnode of DAS4. Please use fs0.das4.cs.vu.nl, fs1.das4.liacs.nl, fs2.das4.science.uva.nl, fs3.das4.tudelft.nl, fs4.das4.science.uva.nl or fs5.das4.astron.nl. Alternatively you can set headNodeOverride if you're sure the headNode you gave is correct.".format( self.headNode, self.name ) )
        if not self.headNode:
            for ip in getIPAddresses():
                hostname = getHostnameByIP(ip)
                if hostname[-6:] == '.vu.nl':
                    self.headNode = 'fs0.das4.vu.nl'
                elif hostname[-9:] == '.liacs.nl':
                    self.headNode = 'fs1.das4.liacs.nl'
                elif hostname[-7:] == '.uva.nl':
                    self.headNode = 'fs4.das4.science.uva.nl'
                elif hostname[-11:] == '.tudelft.nl':
                    self.headNode = 'fs3.das4.tudelft.nl'
                elif hostname[-10:] == '.astron.nl':
                    self.headNode = 'fs5.das4.astron.nl'
            if not self.headNode:
                raise Exception( "No headnode was specified for host {0} and this host was not detected to be in one of the hosting networks. Please specify a headnode.")
    
    def resolveNames(self):
        """
        Resolve any names given in the parameters.
        
        This methods is called after all objects have been initialized.
        """
        host.resolveNames(self)

    #
    # General flow:
    # - prepare
    #        Master only:
    #            Create connection to headnode
    #            Reserve hosts
    #            Create port forwards for all nodes 
    #            Create all slave host objects and set local slave
    #        set temporary bogus self.remoteDirectory if none was specified
    #        host.prepare() to setup a new connection
    #        if the bogus remote dir was used, create a real one with DAS4-specific params, place in self.tempDirectory
    #
    # - setupNewConnection
    #        Always slave connection
    # - sendX:
    #        Much like host:ssh
    # - cleanup
    #        host.cleanup()
    #        Master only:
    #            Call .cleanup() on all slaves before proceeding
    #            Close all port forwards
    #            Cancel reservation
    #            Close master connection
    #

    def setupNewConnection(self):
        """
        Create a new connection to the host.

        The returned object has no specific type, but should be usable as a connection object either by uniquely identifying it or 
        simply by containing the needed information for it.

        Connections created using this function can be closed with closeConnection(...). When cleanup(...) is called all created
        connections will automatically closed and, hence, any calls using those connections will then fail.

        @return The connection object for a new connection. This should be an instance of a subclass of core.host.connectionObject.
        """
        if self.isInCleanup():
            return
        if not self.nodeSet or len(self.nodeSet) < 1:
            return
        if self.nodeSet[0] not in self.secondaryMuxIO:
            connNumber = None
            try:
                das4.muxConnCount__lock.acquire()
                connNumber = das4.muxConnCount
                das4.muxConnCount += 1
            finally:
                das4.muxConnCount__lock.release()
            try:
                self.muxIO__lock[0].acquire()
                self.muxIO[0].write( '+{0}{1}{2}{3}{4}'.format( struct.pack( '!I', connNumber ), struct.pack( '!I', len(self.nodeSet[0]) ), struct.pack( '!I', len('python python_ssh_demux.py') ), self.nodeSet[0], 'python python_ssh_demux.py' ) )
                self.muxIO[0].flush()
            finally:
                self.muxIO__lock[0].release()
            try:
                self.muxIO__lock[1].acquire()
                # Do some reading
                das4MuxConnectionObject.readmux(self.muxIO, self.muxIO__lock, '+')
                # Connection is ready, create and register object
                createSFTP = False
                if self.nodeSet[0] not in self.sftpConnections:
                    client = paramiko.SSHClient()
                    client.load_system_host_keys()
                    try:
                        client.connect( self.headNode, username = self.user )
                    except paramiko.BadHostKeyException:
                        raise Exception( "Bad host key for the headnode of host {0}. Please make sure the host key is already known to the system. The easiest way is usually to just manually use ssh to connect to the remote host once and save the host key.".format( self.name ) )
                    except paramiko.AuthenticationException:
                        raise Exception( "Could not authenticate to the headnode of host {0}. Please make sure that authentication can proceed without user interaction, e.g. by loading an SSH agent or using unencrypted keys.".format( self.name ) )
                    self.sftpConnections[self.nodeSet[0]] = [client]
                    createSFTP = True
                obj = das4MuxConnectionObject( connNumber, self.muxIO, self.muxIO__lock, self.masterConnection, "{0}/das4_sftp/sftp_fwd_{1}".format( self.getPersistentTestDir(), self.nodeSet[0] ), self.sftpConnections[self.nodeSet[0]] )
                self.secondaryMuxIO[self.nodeSet[0]] = (obj, obj, {})
                self.secondaryMuxIO__lock[self.nodeSet[0]] = (threading.RLock(), threading.RLock())
                i = len(self.keepAliveTimers)
                self.keepAliveTimers.append(threading.Timer(30.0, keepAlive, args=[self.secondaryMuxIO[self.nodeSet[0]], self.secondaryMuxIO__lock[self.nodeSet[0]], self, self.keepAliveTimers, i]))
                self.keepAliveTimers[i].start()
                if createSFTP:
                    obj.createSFTPChannel()
                self.muxIO[2][connNumber] = obj
            finally:
                self.muxIO__lock[1].release()
        muxIO_ = self.secondaryMuxIO[self.nodeSet[0]]
        muxIO__lock_ = self.secondaryMuxIO__lock[self.nodeSet[0]]
        connNumber = None
        try:
            das4.muxConnCount__lock.acquire()
            connNumber = das4.muxConnCount
            das4.muxConnCount += 1
        finally:
            das4.muxConnCount__lock.release()
        try:
            muxIO__lock_[0].acquire()
            muxIO_[0].write( '+{0}{1}{2}{3}{4}'.format( struct.pack( '!I', connNumber ), struct.pack( '!I', len(self.nodeSet[0]) ), struct.pack( '!I', len('bash -l') ), self.nodeSet[0], 'bash -l' ) )
            muxIO_[0].flush()
        finally:
            muxIO__lock_[0].release()
        try:
            muxIO__lock_[1].acquire()
            # Do some reading
            das4MuxConnectionObject.readmux(muxIO_, muxIO__lock_, '+')
            # Connection is ready, create and register object
            obj = das4MuxConnectionObject( connNumber, muxIO_, muxIO__lock_, self.masterConnection, "{0}/das4_sftp/sftp_fwd_{1}".format( self.getPersistentTestDir(), self.nodeSet[0] ), self.sftpConnections[self.nodeSet[0]] )
            muxIO_[2][connNumber] = obj
        finally:
            muxIO__lock_[1].release()
        Campaign.debuglogger.log( obj.getIdentification(), 'CREATED in scenario {2} for DAS4 host {0} to node {1} over mux channel'.format( self.name, self.nodeSet[0], self.scenario.name ) )
        try:
            self.connections__lock.acquire()
            if self.isInCleanup():
                obj.close()
                return
            self.connections.append( obj )
        finally:
            try:
                self.connections__lock.release()
            except RuntimeError:
                pass
        # The 'cd' below is absolutely necessary to make sure that automounts and stuff work correctly
        res = self.sendCommand('cd; echo "READY"', obj )
        if not res[-5:] == "READY":
            raise Exception( "Connection to host {0} seems not to be ready after it has been made. Reponse: {1}".format( self.name, res ) )
        return obj

    def setupNewCleanupConnection(self):
        """
        Create a new connection to the host.

        The returned object has no specific type, but should be usable as a connection object either by uniquely identifying it or 
        simply by containing the needed information for it.

        Connections created using this function can be closed with closeConnection(...). When cleanup(...) is called all created
        connections will automatically closed and, hence, any calls using those connections will then fail.

        @return The connection object for a new connection. This should be an instance of a subclass of core.host.connectionObject.
        """
        if self.isInCleanup():
            return
        if not self.nodeSet or len(self.nodeSet) < 1:
            return
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        try:
            client.connect( self.headNode, username = self.user )
        except paramiko.BadHostKeyException:
            raise Exception( "Bad host key for the headnode of host {0}. Please make sure the host key is already known to the system. The easiest way is usually to just manually use ssh to connect to the remote host once and save the host key.".format( self.name ) )
        except paramiko.AuthenticationException:
            raise Exception( "Could not authenticate to the headnode of host {0}. Please make sure that authentication can proceed without user interaction, e.g. by loading an SSH agent or using unencrypted keys.".format( self.name ) )
        trans = client.get_transport()
        chan2 = trans.open_session()
        chan2.set_combine_stderr( True )
        chan2.exec_command( 'ssh {0}'.format( self.nodeSet[0] ) )
        io = (chan2.makefile( 'wb', -1 ), chan2.makefile( 'rb', -1 ) )
        obj = das4ConnectionObject( client, io, "{0}/das4_sftp/sftp_fwd_{1}".format( self.getPersistentTestDir(), self.nodeSet[0] ) )
        Campaign.debuglogger.log( obj.getIdentification(), 'CREATED in scenario {2} for DAS4 host {0} to node {1}'.format( self.name, self.nodeSet[0], self.scenario.name ) )
        try:
            self.connections__lock.acquire()
            if self.isInCleanup():
                obj.close()
                return
            self.connections.append( obj )
        finally:
            try:
                self.connections__lock.release()
            except RuntimeError:
                pass
        # The 'cd' below is absolutely necessary to make sure that automounts and stuff work correctly
        res = self.sendCommand('cd; echo "READY"', obj )
        if not res[-5:] == "READY":
            raise Exception( "Connection to host {0} seems not to be ready after it has been made. Response: {1}".format( self.name, res ) )
        return obj

    def closeConnection(self, connection):
        """
        Close a previously created connection to the host.

        Any calls afterwards to methods of this host with the close connection will fail.
        
        The default implementation will close the connection if it wasn't already closed
        and remove it from self.connections.

        @param  The connection to be closed.
        """
        Campaign.debuglogger.closeChannel(connection.getIdentification())
        host.closeConnection(self, connection)

    def sendCommandAsyncStart(self, command, reuseConnection):
        """
        Sends a bash command to the remote host without waiting for the answer.
        
        Note that it is imperative that you call sendCommandAsyncEnd(...) after this call, or you will screw up your connection!

        Be sure to call connection.setInAsync() as well.

        @param  command             The command to be executed on the remote host.
        @param  reuseConnection     A specific connection object as obtained through setupNewConnection(...) to reuse that connection.
                                    Contrary to other methods True of False are explicitly not accepted.
        """
        connection = None
        try:
            connection = self.getConnection(reuseConnection)
            if connection.isInAsync():
                Campaign.logger.log( "WARNING! Connection {0} of host {1} started an async command, but an async command was still running.".format( connection.getIdentification(), self.name ), True )
                Campaign.logger.localTraceback( True )
                res = self.sendCommandAsyncEnd(connection)
                Campaign.logger.log( "WARNING! Output of ending the connection: {0}".format( res ), True )
                connection.outOfOrderResult = res
            connection.write( command+'\n# `\n# \'\n# "\necho "\nblabladibla__156987349253457979__noonesGonnaUseThis__right__p2ptestframework"\n' )
            connection.setInAsync()
            Campaign.debuglogger.log( connection.getIdentification(), 'SEND {0}'.format( command ) )
        finally:
            self.releaseConnection(reuseConnection, connection)

    def sendCommandAsyncEnd(self, reuseConnection):
        """
        Retrieves the response to a bash command to the remote host that was sent earlier on.
        
        Note that this must not be called other than directly after sendCommandAsyncStart(...).
        Do not call on just any connection or you will screw it up!

        Be sure to call connection.clearInAsync() as well.

        @param  reuseConnection     A specific connection object as obtained through setupNewConnection(...) to reuse that connection.
                                    Contrary to other methods True of False are explicitly not accepted.
        
        @return The result from the command. The result is stripped of leading and trailing whitespace before being returned.
        """
        connection = None
        try:
            connection = self.getConnection(reuseConnection)
            if not connection.isInAsync():
                Campaign.logger.log( "WARNING! Connection {0} of host {1} ended an async command, but none was running. Returning ''.".format( connection.getIdentification(), self.name ), True )
                Campaign.logger.localTraceback(True)
                res = connection.outOfOrderResult
                connection.outOfOrderResult = ''
                return res
            res = ''
            line = connection.readline()
            while line != '' and line.strip() != 'blabladibla__156987349253457979__noonesGonnaUseThis__right__p2ptestframework':
                Campaign.debuglogger.log( connection.getIdentification(), 'RECV {0}'.format( line ) )
                res += line
                line = connection.readline()
            connection.clearInAsync()
            # Return output (ditch the last trailing \n)
            return res.strip()
        finally:
            self.releaseConnection(reuseConnection, connection)

    def sendFile(self, localSourcePath, remoteDestinationPath, overwrite = False, reuseConnection = True):
        """
        Sends a file to the remote host.

        Regarding reuseConnection it is possible the value may be ignored: a new connection may be needed for file transfer, anyway.

        @param  localSourcePath         Path to the local file that is to be sent.
        @param  remoteDestinationPath   Path to the destination file on the remote host.
        @param  overwrite               Set to True to not raise an Exception if the destination already exists.
        @param  reuseConnection         True to try and reuse the default connection for sending the file.
                                        False to build a new connection for sending this file and use that.
                                        A specific connection object as obtained through setupNewConnection(...) to reuse that connection.
        """
        connection = None
        try:
            connection = self.getConnection(reuseConnection)
            newConnection = connection.createSFTPChannel()
            try:
                sftp = connection.sftpChannel
                if das4ConnectionObject.existsRemote(sftp, remoteDestinationPath):
                    if not overwrite: 
                        raise Exception( "Sending file {0} to {1} on host {2} without allowing overwrite, but the destination already exists".format( localSourcePath, remoteDestinationPath, self.name ) )
                    elif das4ConnectionObject.isRemoteDir(sftp, remoteDestinationPath):
                        raise Exception( "Sending file {0} to {1} on host {2} with overwrite, but the destination already exsits and is a directory".format( localSourcePath, remoteDestinationPath, self.name ) )
                if self.isInCleanup():
                    return
                Campaign.debuglogger.log( connection.getIdentification(), 'SFTP SEND FILE {0} TO {1}'.format( localSourcePath, remoteDestinationPath ) )
                sftp.put( localSourcePath, remoteDestinationPath )
                sftp.chmod( remoteDestinationPath, os.stat(localSourcePath).st_mode )
            finally:
                if newConnection:
                    connection.removeSFTPChannel()
        finally:
            self.releaseConnection(reuseConnection, connection)

    def sendFiles(self, localSourcePath, remoteDestinationPath, reuseConnection = True):
        """
        Sends a directory to the remote host.

        This will recursively send the local directory and all its contents to the remote host.

        Example:    sendFiles( '/home/me/myLocalDir', '/tmp/myTmpDir/newRemoteDir' )
        If newRemoteDir does not already exist then it will be created. A file /home/me/myLocalDir/x will end up
        on the remote host as /tmp/myTmpDir/newRemoteDir/x .

        This method will always overwrite existing files.

        Regarding reuseConnection it is possible the value may be ignored: a new connection may be needed for file transfer, anyway.

        The default implementation will recursively call sendFile or sendFiles on the contents of the
        local directory.

        @param  localSourcePath         Path to the local directory that is to be sent.
        @param  remoteDestinationPath   Path to the destination directory on the remote host.
        @param  reuseConnection         True to try and reuse the default connection for sending the file.
                                        False to build a new connection for sending this file and use that.
                                        A specific connection object as obtained through setupNewConnection(...) to reuse that connection.
        """
        connection = None
        try:
            connection = self.getConnection(reuseConnection)
            newConnection = connection.createSFTPChannel()
            try:
                sftp = connection.sftpChannel
                paths = [(localSourcePath, remoteDestinationPath)]
                while len(paths) > 0:
                    localPath, remotePath = paths.pop()
                    if self.isInCleanup():
                        return
                    if os.path.isdir( localPath ):
                        if not das4ConnectionObject.existsRemote(sftp, remotePath):
                            Campaign.debuglogger.log( connection.getIdentification(), 'SFTP CREATE REMOTE DIR {0}'.format( remotePath ) )
                            sftp.mkdir( remotePath )
                            sftp.chmod( remotePath, os.stat(localPath).st_mode )
                        paths += [(os.path.join( localPath, path ), '{0}/{1}'.format( remotePath, path )) for path in os.listdir( localPath )]
                    else:
                        if das4ConnectionObject.existsRemote(sftp, remotePath) and das4ConnectionObject.isRemoteDir(sftp, remotePath):
                            raise Exception( "Sending file {0} to {1} on host {2} with overwrite, but the destination already exsits and is a directory".format( localPath, remotePath, self.name ) )
                        Campaign.debuglogger.log( connection.getIdentification(), 'SFTP SEND FILE {0} TO {1}'.format( localPath, remotePath ) )
                        sftp.put( localPath, remotePath )
                        sftp.chmod( remotePath, os.stat(localPath).st_mode )
            finally:
                if newConnection:
                    connection.removeSFTPChannel()
        finally:
            self.releaseConnection(reuseConnection, connection)

    def getFile(self, remoteSourcePath, localDestinationPath, overwrite = False, reuseConnection = True):
        """
        Retrieves a file from the remote host.

        Regarding reuseConnection it is possible the value may be ignored: a new connection may be needed for file transfer, anyway.

        @param  remoteSourcePath        Path to the file to be retrieved on the remote host.
        @param  localDestinationPath    Path to the local destination file.
        @param  overwrite               Set to True to not raise an Exception if the destination already exists.
        @param  reuseConnection         True to try and reuse the default connection for sending the file.
                                        False to build a new connection for sending this file and use that.
                                        A specific connection object as obtained through setupNewConnection(...) to reuse that connection.
        """
        if os.path.exists( localDestinationPath ):
            if not overwrite:
                raise Exception( "Getting file {0} to {1} from host {2} without allowing overwrite, but the destination already exists".format( remoteSourcePath, localDestinationPath, self.name ) )
            elif os.path.isdir( localDestinationPath ):
                raise Exception( "Getting file {0} to {1} from host {2} with overwrite, but the destination already exists and is a directory".format( remoteSourcePath, localDestinationPath, self.name ) )
        if self.isInCleanup():
            return
        connection = None
        try:
            connection = self.getConnection(reuseConnection)
            newConnection = connection.createSFTPChannel()
            try:
                sftp = connection.sftpChannel
                if self.isInCleanup():
                    return
                Campaign.debuglogger.log( connection.getIdentification(), 'SFTP RETRIEVE FILE {0} TO {1}'.format( remoteSourcePath, localDestinationPath ) )
                sftp.get( remoteSourcePath, localDestinationPath )
            finally:
                if newConnection:
                    connection.removeSFTPChannel()
        finally:
            self.releaseConnection(reuseConnection, connection)

    def sendMasterCommand(self, command):
        """
        Sends a bash command to the remote host.

        @param  command             The command to be executed on the remote host.

        @return The result from the command. The result is stripped of leading and trailing whitespace before being returned.
        """
        # Send command
        self.masterIO[0].write( command+'\n# `\n# \'\n# "\necho "\nblabladibla__156987349253457979__noonesGonnaUseThis__right__p2ptestframework"\n' )
        Campaign.debuglogger.log('das4_master', 'SEND {0}'.format( command ) )
        # Read output of command
        res = ''
        line = self.masterIO[1].readline()
        while line != '' and line.strip() != 'blabladibla__156987349253457979__noonesGonnaUseThis__right__p2ptestframework':
            Campaign.debuglogger.log('das4_master', 'RECV {0}'.format( line ) )
            res += line
            line = self.masterIO[1].readline()
        # Return output (ditch the last trailing \n)
        return res.strip()

    def prepare(self):
        """
        Execute commands on the remote host needed for host specific preparation.

        The default implementation simply ensures the existence of a remote directory.
        """
        if self.isInCleanup():
            return
        if not self.nodeSet:
            # Supervisor host
            # Check sanity of all DAS4 hosts
            for h in [h for h in self.scenario.getObjects('host') if isinstance(h, das4)]:
                if h.headNode != self.headNode:
                    raise Exception( "When multiple DAS4 host objects are declared, they must all share the same headnode. Two different headnodes have been found: {0} and {1}. This is unsupported.".format( self.headNode, h.headNode ) )
            # Find python mux file
            demux_script = os.path.join( Campaign.testEnvDir, 'Utils', 'python_ssh_demux', 'python_ssh_demux.py' )
            if not os.path.exists(demux_script):
                raise Exception( "For running the DAS4 the python_ssh_demux utility script is expected in {0}".format( demux_script ) )
            # Create connection to headnode
            self.masterConnection = paramiko.SSHClient()
            self.masterConnection.load_system_host_keys()
            try:
                self.masterConnection.connect( self.headNode, username = self.user )
            except paramiko.BadHostKeyException:
                raise Exception( "Bad host key for host {0}. Please make sure the host key of the headnode is already known to the system. The easiest way is usually to just manually use ssh to connect to the headnode once and save the host key.".format( self.name ) )
            except paramiko.AuthenticationException:
                raise Exception( "Could not authenticate to host {0}. Please make sure that authentication can proceed without user interaction, e.g. by loading an SSH agent or using unencrypted keys.".format( self.name ) )
            trans = self.masterConnection.get_transport()
            chan2 = trans.open_session()
            chan2.set_combine_stderr( True )
            chan2.exec_command( 'bash -l' )
            self.masterIO = (chan2.makefile( 'wb', -1 ), chan2.makefile( 'rb', -1 ))
            Campaign.debuglogger.log( 'das4_master', 'CREATED in scenario {2} for DAS4 host {0} to headnode {1}'.format( self.name, self.headNode, self.scenario.name ) )
            masterSFTP = self.masterConnection.open_sftp()
            Campaign.debuglogger.log( 'das4_master', 'SFTP CHANNEL CREATED' )
            masterSFTP.put( demux_script, 'python_ssh_demux.py' )
            Campaign.debuglogger.log( 'das4_master', 'SFTP SEND FILE {0} TO {1}'.format( demux_script, 'python_ssh_demux.py' ) )
            masterSFTP.close()
            Campaign.debuglogger.log( 'das4_master', 'SFTP CHANNEL REMOVED' )
            chan2 = trans.open_session()
            chan2.set_combine_stderr( True )
            chan2.exec_command( 'python python_ssh_demux.py' )
            self.muxIO = (chan2.makefile( 'wb', -1), chan2.makefile( 'rb', -1 ), {})
            Campaign.debuglogger.log( 'das4_master', 'MUX CHANNEL CREATED' )
            i = len(self.keepAliveTimers)
            self.keepAliveTimers.append(threading.Timer(30.0, keepAlive, args=[self.muxIO, self.muxIO__lock, self, self.keepAliveTimers, i]))
            self.keepAliveTimers[i].start()
            self.sendMasterCommand('module load prun')
            # Reserve nodes
            totalNodes = sum([h.nNodes for h in self.scenario.getObjects('host') if isinstance(h, das4)])
            maxReserveTime = max([h.reserveTime for h in self.scenario.getObjects('host') if isinstance(h, das4)])
            if self.isInCleanup():
                Campaign.debuglogger.closeChannel( 'das4_master' )
                self.masterConnection.close()
                del self.masterConnection
                self.masterConnection = None
                return
            self.reservationID = self.sendMasterCommand('preserve -1 -# {0} {1} | grep "Reservation number" | sed -e "s/^Reservation number \\([[:digit:]]*\\):$/\\1/" | grep -E "^[[:digit:]]*$"'.format( totalNodes, maxReserveTime ) )
            if self.reservationID == '' or not isPositiveInt( self.reservationID ):
                Campaign.debuglogger.closeChannel( 'das4_master' )
                self.masterConnection.close()
                del self.masterConnection
                self.masterConnection = None
                res = self.reservationID
                self.reservationID = None
                raise Exception( 'The reservationID as found on the DAS4 ("{0}") seems not to be a reservation ID.'.format( res ) )
            print "Reservation ID {1} on DAS4 made for {0} nodes, waiting for availability".format( totalNodes, self.reservationID )
            # Wait for nodes
            # Please note how the following command is built: one string per line, but all these strings will be concatenated.
            # The whitespace is *purely* readability and *not* present in the resulting command. Hence the "; " at the end of each of these strings.
            while True: 
                res = self.sendMasterCommand( (
                        "ERR=0; "
                        "COUNT=0; "
                        'while ! qstat -j {0} | grep "usage" {devnull}; do '
                            'if ! qstat -j {0} {devnull}; then '
                                'echo "ERR"; ERR=1; break; '
                            'fi; '
                            'sleep 1; '
                            'if [ $COUNT -gt 3 ]; then '
                                'echo "TIME"; ERR=1; break; '
                            'fi; '
                            'COUNT=$(($COUNT + 1)); '
                        'done; '
                        'if [ $ERR -eq 0 ]; then '
                            'while ! preserve -llist | grep -E "^{0}[[:space:]]" | sed -e "s/^{d}{s}{ns}{s}{ns}{s}{ns}{s}{ns}{s}{ns}{s}r{s}{d}{s}\\(.*\\)$/\\1/" | grep -v -E "^{0}[[:space:]]" {devnull}; do '
                                'if ! qstat -j {0} {devnull}; then '
                                    'echo "ERR"; ERR=1; break; '
                                'fi; '
                                'sleep 1; '
                            'done; '
                        'fi; '
                        'if [ $ERR -eq 0 ]; then '
                            'echo "OK"; '
                        'fi'
                        ).format( self.reservationID,
                                      devnull = '>/dev/null 2>/dev/null',
                                      s = '[[:space:]][[:space:]]*', 
                                      d = '[[:digit:]][[:digit:]]*', 
                                      ns = '[^[:space:]][^[:space:]]*' ) )
                if res == 'TIME':
                    continue
                if self.isInCleanup():
                    return
                break
            if res != "OK":
                raise Exception( "Nodes for host {0} never became available and the reservation seems to be gone as well.".format( self.name ) )
            if self.isInCleanup():
                return
            # Get the nodes
            nodes = self.sendMasterCommand( 'preserve -llist | grep -E "^{0}[[:space:]]" | sed -e "s/^{d}{s}{ns}{s}{ns}{s}{ns}{s}{ns}{s}{ns}{s}r{s}{d}{s}\\(.*\\)$/\\1/"'.format(
                                              self.reservationID,
                                              s = '[[:space:]][[:space:]]*', 
                                              d = '[[:digit:]][[:digit:]]*', 
                                              ns = '[^[:space:]][^[:space:]]*' ) )
            if nodes == '':
                raise Exception( "Nodes for host {0} could not be extracted from the reservation.".format( self.name ) )
            wrongStart = "{0} ".format( self.reservationID )
            if nodes[:len(wrongStart)] == wrongStart:
                raise Exception( "Nodes for host {0} could not be extracted from the reservation. Nodes found (and presumed to be incorrect): {1}.".format( self.name, nodes ) )
            nodeList = nodes.split()
            # See if we can reach all nodes
            for node in nodeList:
                if self.isInCleanup():
                    return
                res = self.sendMasterCommand( 'if ! qstat -j {1} > /dev/null 2> /dev/null; then echo "ERR"; else ssh -n -T -o BatchMode=yes {0} "echo \\"OK\\""; fi'.format( node, self.reservationID ) ) 
                if res.splitlines()[-1] != "OK":
                    raise Exception( "Can't connect to a node of host {0}. Observed output: {1}".format( self.name, res ) )
            print "Nodes on DAS4 available: {0}".format( nodes )
            # Divide all nodes over the master hosts
            counter = 0
            for h in [h for h in self.scenario.getObjects('host') if isinstance( h, das4 )]:
                nextCounter = counter + h.nNodes
                if nextCounter > len(nodeList):
                    raise Exception( "Handing out nodes from host {0} to the DAS4 host objects. Trying to hand out nodes numbered {1} to {2} (zero-bases), but there are only {3} nodes reserved. Insanity!".format( self.name, counter, nextCounter, totalNodes ) )
                h.nodeSet = nodeList[counter:nextCounter]
                h.masterConnection = self.masterConnection
                h.masterIO = self.masterIO
                h.sftpConnections = self.sftpConnections
                h.muxIO = self.muxIO
                h.muxIO__lock = self.muxIO__lock
                h.secondaryMuxIO = self.secondaryMuxIO
                h.secondaryMuxIO__lock = self.secondaryMuxIO__lock
                counter = nextCounter
            if counter != len(nodeList):
                raise Exception( "After handing out all the nodes to the DAS4 host objects from host {0}, {1} nodes have been handed out, but {2} were reserved. Insanity!".format( self.name, counter, totalNodes ) )
            # / Supervisor host
        if self.nNodes:
            # Master host part 1
            # Create temporary persistent directory, if needed
            if not self.remoteDirectory:
                if self.isInCleanup():
                    Campaign.debuglogger.closeChannel( 'das4_master' )
                    self.masterConnection.close()
                    del self.masterConnection
                    self.masterConnection = None
                    return
                self.tempPersistentDirectory = self.sendMasterCommand('mktemp -d --tmpdir="`pwd`"')
                if not self.tempPersistentDirectory or self.tempPersistentDirectory == '':
                    self.tempPersistentDirectory = None
                    raise Exception( "Could not create temporary persistent directory on the headnode for host {0}".format( self.name ) )
                res = self.sendMasterCommand('[ -d "{0}" ] && echo "OK" || echo "ERR"'.format( self.tempPersistentDirectory ))
                if res.splitlines()[-1] != "OK":
                    res1 = self.tempPersistentDirectory
                    self.tempPersistentDirectory = None
                    raise Exception( "Could not verify the existence of the temporary persistent directory on the headnode for host {0}. Response: {1}. Response to the test: {2}.".format( self.name, res1, res ) )
            # Create all slave hosts for this master host
            self.slaves = []
            executions = [e for e in self.scenario.getObjects('execution') if e.host == self]
            for c in range( 1, len(self.nodeSet) ):
                # Create slave host object
                newObj = das4(self.scenario)
                newObj.headNode = self.headNode
                newObj.user = self.user
                newObj.tempPersistentDirectory = self.tempPersistentDirectory
                newObj.nodeSet = [self.nodeSet[c]]
                newObj.masterConnection = self.masterConnection
                newObj.masterIO = self.masterIO
                newObj.sftpConnections = self.sftpConnections
                newObj.muxIO = self.muxIO
                newObj.muxIO__lock = self.muxIO__lock
                newObj.secondaryMuxIO = self.secondaryMuxIO
                newObj.secondaryMuxIO__lock = self.secondaryMuxIO__lock
                newObj.copyhost(self)
                newName = "{0}!{1}".format( self.name, c )
                if newName in self.scenario.getObjectsDict('host'):
                    raise Exception( "Insanity! DAS4 host {0} wanted to create slave for connection {1}, which would be named {2}, but a host with that name already exists!".format( self.name, c, newName ) )
                newObj.name = newName
                if self.isInCleanup():
                    return
                self.scenario.objects['host'][newObj.getName()] = newObj
                self.slaves.append(newObj)
                # Duplicate each execution with this host for the slave host
                for e in executions:
                    ne = core.execution.execution(self.scenario)
                    ne.hostName = newName
                    ne.clientName = e.clientName
                    ne.fileName = e.fileName
                    if e.parserNames:
                        ne.parserNames = list(e.parserNames)
                    else:
                        ne.parserNames = None
                    ne.seeder = e.seeder
                    ne.checkSettings()
                    ne.resolveNames()
                    if self.isInCleanup():
                        return
                    self.scenario.objects['execution'][ne.getName()] = ne
                    if ne.client not in newObj.clients:
                        newObj.clients.append( ne.client )
                    if ne.file not in newObj.files:
                        newObj.files.append( ne.file )
                    if ne.isSeeder() and ne.file not in newObj.seedingFiles:
                        newObj.seedingFiles.append( ne.file )
            # Create sftp forwarding scripts
            if self.isInCleanup():
                return
            res = self.sendMasterCommand('mkdir -p "{0}/das4_sftp" && echo "OK"'.format( self.getPersistentTestDir( ) ))
            if res.splitlines()[-1] != "OK":
                raise Exception( "Failed to create the SFTP forwarding scripts directory on the headnode of host {0}: {1}".format( self.name, res ) )
            for node in self.nodeSet:
                if self.isInCleanup():
                    return
                res = self.sendMasterCommand('echo "ssh -o BatchMode=yes -s {0} sftp" > "{1}/das4_sftp/sftp_fwd_{0}" && chmod +x "{1}/das4_sftp/sftp_fwd_{0}" && echo "OK"'.format( node, self.getPersistentTestDir( ) ) )
                if res.splitlines()[-1] != "OK":
                    raise Exception( "Failed to create the SFTP forwarding script for node {1} on the headnode of host {0}: {2}".format( self.name, node, res ) )
            # / Master host part 1
        # Slave host
        # Prevent host.prepare(self) from creating a temp dir
        self.bogusRemoteDir = False
        if not self.remoteDirectory:
            self.remoteDirectory = 'bogus'
            self.bogusRemoteDir = True
        # Run host.prepare(self)
        if self.isInCleanup():
            return
        try:
            host.prepare(self)
        finally:
            if self.bogusRemoteDir:
                self.remoteDirectory = None
        # Create a local storage temp dir if needed
        if self.bogusRemoteDir:
            if self.isInCleanup():
                return
            self.tempDirectory = self.sendCommand( 'mkdir -p /local/{0}; mktemp -d --tmpdir=/local/{0}'.format( self.user ) )
            if self.tempDirectory != '':
                testres = self.sendCommand( '[ -d "{0}" ] && [ `ls -a "{0}" | wc -l` -eq 2 ] && echo "OK"'.format( self.tempDirectory ) )
            if self.tempDirectory == '' or testres.strip() != "OK":
                res = self.tempDirectory
                self.tempDirectory = None
                raise Exception( "Could not correctly create a remote temporary directory on host {1} or could not verify it. Response: {0}\nResponse to the verification: {2}".format( res, self.name, testres ) )
        # / Slave host
        if self.nNodes:
            # Master host part 2
            # Prepare all the slave hosts
            for s in self.slaves:
                if self.isInCleanup():
                    return
                s.prepare()
            # / Master host part 2

    def cleanup(self, reuseConnection = None):
        """
        Executes commands to do host specific cleanup.
        
        The default implementation removes the remote temporary directory, if one was created.

        Subclassers are advised to first call this implementation and then proceed with their own steps.
        
        @param  reuseConnection If not None, force the use of this connection object for commands to the host.
        """
        host.cleanup(self, reuseConnection)
        if self.reservationID:
            # Supervisor host
            if len([h_ for h_ in self.scenario.getObjects('host') if isinstance(h_, das4) and not h_.isInCleanup()]) > 0:
                # Give them a second to start their own cleanup first
                time.sleep(1)
            for h in [h for h in self.scenario.getObjects('host') if isinstance(h, das4)]:
                if not h.isInCleanup():
                    try:
                        h.cleanup()
                    except Exception as e:
                        Campaign.logger.log("Ignoring exception while trying to run cleanup on other DAS4 host: {0}".format( e.__str__()))
                        Campaign.logger.exceptionTraceback()
            if self.slaves and len(self.slaves) > 0:
                for h in self.slaves:
                    if not h.isInCleanup():
                        h.cleanup()
            self.sendMasterCommand( 'preserve -c {0}'.format( self.reservationID ) )
            for t in self.keepAliveTimers:
                try:
                    t.cancel()
                except Exception:
                    pass
            delset = [hostname for hostname in self.secondaryMuxIO]
            for hostname in delset:
                gotLock = False
                startTime = time.time()
                try:
                    while not self.secondaryMuxIO__lock[hostname][0].acquire(False):
                        if time.time() - startTime > 15:
                            Campaign.logger.log( "WARNING! Could not acquire write lock on the mux channel. The secondary demux channel on {0} has not been signalled to kill.".format( hostname ) )
                            break
                        time.sleep(1)
                    else:
                        gotLock = True
                    if gotLock:
                        self.secondaryMuxIO[hostname][0].write('X\n')
                        self.secondaryMuxIO[hostname][0].flush()
                finally:
                    if gotLock:
                        self.secondaryMuxIO__lock[hostname][0].release()
                del self.secondaryMuxIO[hostname][0]
                del self.secondaryMuxIO[hostname][1]
                del self.secondaryMuxIO[hostname]
                del self.secondaryMuxIO__lock[hostname]
            gotLock = False
            startTime = time.time()
            try:
                while not self.muxIO__lock[0].acquire(False):
                    if time.time() - startTime > 15:
                        Campaign.logger.log( "WARNING! Could not acquire write lock on the mux channel. The primary demux channel on the headnode has not been signalled to kill." )
                        break
                    time.sleep(1)
                else:
                    gotLock = True
                if gotLock:
                    self.muxIO[0].write('X\n')
                    self.muxIO[0].flush()
            finally:
                if gotLock:
                    self.muxIO__lock[0].release()
            del self.muxIO
            self.muxIO = None
            Campaign.debuglogger.log( 'das4_master', 'MUX CHANNEL REMOVED' )
            showError = False
            if self.tempPersistentDirectory:
                res = self.sendMasterCommand('rm -rf "{0}" && echo "OK"'.format( self.tempPersistentDirectory ) )
                if res.splitlines()[-1] != "OK":
                    showError = True
                self.tempPersistentDirectory = None
            del self.masterIO
            self.masterIO = None
            Campaign.debuglogger.closeChannel( 'das4_master' )
            self.masterConnection.close()
            del self.masterConnection
            self.masterConnection = None
            delset = [node for node in self.sftpConnections]
            for node in delset:
                if len(self.sftpConnections[node]) > 1 and self.sftpConnections[node][1] is not None:
                    self.sftpConnections[node][1].close()
                self.sftpConnections[node][0].close()
                del self.sftpConnections[node][0]
                if len(self.sftpConnections[node]) > 1:
                    del self.sftpConnections[node][1]
                del self.sftpConnections[node]
            if showError:
                raise Exception( "Could not remove the persistent temporary directory {3} from the DAS4 in host {0}. Reponse: {1}".format( self.name, res, self.tempPersistentDirectory ) )
            # / Supervisor host
        else:
            # Non-supervisor host (both master and slave)
            showError = False
            if not self.reservationID and self.nNodes:
                # Master host (and not supervisor)
                if self.slaves and len(self.slaves) > 0:
                    for h in self.slaves:
                        if not h.isInCleanup():
                            h.cleanup()
                if self.tempPersistentDirectory:
                    res = self.sendMasterCommand('rm -rf "{0}" && echo "OK"'.format( self.tempPersistentDirectory ) )
                    if res.splitlines()[-1] != "OK":
                        showError = True
                    self.tempPersistentDirectory = None
                # / Master host
            del self.muxIO
            self.muxIO = None
            del self.masterIO
            self.masterIO = None
            del self.masterConnection
            self.masterConnection = None
            if showError:
                raise Exception( "Could not remove the persistent temporary directory {3} from the DAS4 in host {0}. Reponse: {1}".format( self.name, res, self.tempPersistentDirectory ) )
            # / Non-supervisor host

    def getTestDir(self):
        """
        Returns the path to the directory on the remote host where (temporary) files are stored for the testing
        environment.

        Files placed in this directory are not guaranteed to remain available for later downloading.
        This is the perfect location for files such as data to be downloaded by clients, which can be forgotten
        the moment the client finishes.
        For logfiles and other files that are needed after the execution of the client, use
        getPersistentTestDir().

        During cleanup this may return None! 

        The default implementation uses self.remoteDirectory if it exists, or otherwise self.tempDirectory.

        @return The test directory on the remote host.
        """
        if self.remoteDirectory and not self.bogusRemoteDir:
            return self.remoteDirectory
        return self.tempDirectory

    def getPersistentTestDir(self):
        """
        Returns the path to the directory on the remote host where (temporary) files are stored for the testing
        environment, which will remain available until the host is cleaned.

        Note that persistence in this case is limited to the complete test as opposed to data being thrown away
        at any possible moment in between commands.

        During cleanup this may return None! 

        The default implementation just uses self.getTestDir() and is hence under the assumption that the
        normal test dir is persistent enough.

        @return The persisten test directory on the remote host.
        """
        if self.tempPersistentDirectory:
            return self.tempPersistentDirectory
        return self.getTestDir()

    def getSubNet(self):
        """
        Return the subnet of the external addresses of the host.

        @return The subnet of the host(s).
        """
        return self.nodeSet[0]

    def getAddress(self):
        """
        Return the single address (IP or hostname) of the remote host, if any.

        An obvious example of this method returning '' would be a host implementation that actually uses a number
        of remote hosts in one host object: one couldn't possibly return exactly one address for that and be
        correct about it in the process.

        Default implementation just returns ''.

        @return The address of the remote host, or '' if no such address can be given.
        """
        return self.nodeSet[0]

    @staticmethod
    def APIVersion():
        return "2.2.0"
