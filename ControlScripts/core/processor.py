import os

from core.campaign import Campaign
from core.coreObject import coreObject

def parseError( msg ):
    raise Exception( "Parse error for processor object on line {0}: {1}".format( Campaign.currentLineNumber, msg ) )

class processor(coreObject):
    """
    The parent class for all processors.

    This object contains all the default implementations for every processor.
    When subclassing processor be sure to use the skeleton class as a basis: it saves you a lot of time.
    """

    number = None           # The number of this processor

    # @static
    processorCount = 0      # The total number of processors

    def __init__(self, scenario):
        """
        Initialization of a generic processor object.

        @param  scenario        The ScenarioRunner object this processor object is part of.
        """
        coreObject.__init__(self, scenario)
        self.number = processor.processorCount
        processor.processorCount += 1

    # This method has unused arguments; that's fine
    # pylint: disable-msg=W0613
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
        parseError( 'Unknown parameter name: {0}'.format( key ) )
    # pylint: enable-msg=W0613

    def checkSettings(self):
        """
        Check the sanity of the settings in this object.

        This method is called after all calls to parseSetting(...) have been done.
        Any defaults may be set here as well.

        An Exception is raised in the case of insanity.
        """
        pass

    def resolveNames(self):
        """
        Resolve any names given in the parameters.
        
        This methods is called after all objects have been initialized.
        """
        pass

    # This method has unused arguments; that's fine
    # pylint: disable-msg=W0613
    def processLogs(self, baseDir, outputDir):
        """
        Process the raw and parsed logs found in the base directory.

        The raw logs are found in self.getRawLogDir( execution, baseDir ).
        The parsed logs are found in self.getParsedLogDir( execution, baseDir ).

        Be sure to document in the header of your module which (parsed) logs you expect to be present and with which filename.

        Subclassers must override this method.

        @param  baseDir     The base directory for the logs.
        @param  outputDir   The path to the directory on the local machine where the processed logs are to be stored.
        """
        raise Exception( "Not implemented" )
    # pylint: enable-msg=W0613

    @staticmethod
    def getRawLogDir(execution, baseDir):
        """
        Returns the directory with the raw logs for the given execution under the given baseDir.

        @param  execution   The execution for which the raw logs are to be found.
        @param  baseDir     The base directory where the logs should reside.
        
        @return The path to the directory with the raw logs.
        """
        return os.path.join( baseDir, 'exec_{0}'.format( execution.getNumber() ), 'logs' )

    @staticmethod
    def getParsedLogDir(execution, baseDir):
        """
        Returns the directory with the parsed logs for the given execution under the given baseDir.

        @param  execution   The execution for which the parsed logs are to be found.
        @param  baseDir     The base directory where the logs should reside.

        @return The path to the directory with the parsed logs.
        """
        return os.path.join( baseDir, 'exec_{0}'.format( execution.getNumber() ), 'parsedLogs' )

    def getModuleType(self):
        """
        Return the moduleType string.
        
        @return    The module type.
        """
        return 'processor'
    
    def getName(self):
        """
        Return the name of the object.
        
        @return    The name.
        """
        return self.number
    
    def canReprocess(self):
        """
        Return whether this processor can be used to reprocess after a run has already been torn down.
        
        This mainly signals that this parser functions within the following constraints:
        - resolveNames is never called
        - host, client and file object are explicitly unavailable
        - Only part of the scenario object is available:
            - scenario.isFake() is available and returns True
            - scenario.name is available and correct
            - scenario.getObjects(...) is available and will return all executions but an empty list otherwise
            - scenario.getObjectsDict(...) is available and will return all executions but an empty dictionary otherwise
            - The executions returned by this scenario are limited as described below
            - The methods are not available during initialization
        - Only part of the static Campaign object is available:
            - Campaign.logger is available as normally and logs to stdout
            - Campaign.which is available as normally
        - Only part of the execution object is available:
            - execution.isFake() is available and returns True
            - execution.getNumber() is available and limited
            - execution.client is available but incomplete
                - execution.client.name is available and reads '__reparse__'
                - execution.client.isSideService() is available
                    - returns True unless any log exists for the execution
            - execution.timeout is available and 0.0 unless the data was saved using processor:savetimeout
            - execution.isSeeder() is available and False unless the data was saved using processor:isSeeder (and this was a seeder)
            - execution.host is available but limited 
                - execution.host.name is available and reads '__reparse__' unless the data was saved using processor:savehostname
        
        @return    True iff this processor can reprocess.
        """
        return False

    @staticmethod
    def APIVersion():
        return "2.4.0-core"
