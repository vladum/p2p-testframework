#!/bin/bash

#
#
# Requires: logError
# Provides: reinitializeCleanup, fail, cleanup, addCleanupCommand, addCleanupScript, removeCleanupScript, signalFail, checkFail, checkFailReturn, cleanFailSignal
#


# === Cleanup functions ===
# The cleanup functions can and should be called when things go awry or when everything is done:
# correct cleanup is then provided and the script is ended

##
# Reinitializes the general cleanup script
##
function reinitializeCleanup() {
    ## The array with cleanup scripts
    CLEANUP_MAX_INDEX=0
    CLEANUP_TMP_FILES=([0]=`mktemp`)
    if [ -z ${CLEANUP_TMP_FILES[0]} ]; then
        logError "Could not create general cleanup script file" 1>&2
        exit -1
    fi
}

# Reinitialize now if general cleanup file is not initialized
if [ "XX" = "X${CLEANUP_TMP_FILES[0]}X" ]; then
    reinitializeCleanup
fi

## 
# Signals a failure.
# Will clean everything up and exit with the given status or -1 if none was given.
# This function will never return.
#
# @param    Optional status to pass to exit
##
function fail() {
    cleanup
    echo "Testing environment failed partially or fully. Please check the above log and any campaign logs or other logs to find out why." 1>&2
    echo "A stack trace of function calls follows." 1>&2
    local frame=0
    while caller $frame 1>&2; do
        frame=$(($frame + 1))
    done
    if [ $# -eq 0 ]; then   
        exit -1
    fi
    exit $1
}

##
# Checks whether the failure signal file has been created.
# Will fail if said file has been created and never return, then.
# A no-op if no failure signal file has been created.
#
# See also checkFailReturn, which can be used instead.
#
# Be sure to call this after calling a potentially failing function in a subscript (such as using `` for functions that output their value rather than returning it)
##
function checkFail() {
    if checkFailReturn; then
        cleanFailSignal
        fail
    fi
}

##
# Checks whether the failure signal file has been created.
# Will return true (0) if it has and false (1) otherwise.
#
# See also checkFail, which can be used instead.
#
# When checking and handling failure manually, be sure to call cleanFailSignal when failing.
#
# @return   True (0) iff the testing environment should fail.
##
function checkFailReturn() {
    if [ -f "${LOCAL_TEST_DIR}/__fail__signal__file" ]; then
        return 0
    fi
    return 1
}

##
# Cleans the failure signal set by signalFail and checked by checkFail or checkFailReturn.
##
function cleanFailSignal() {
    rm -f "${LOCAL_TEST_DIR}/__fail__signal__file"
}

##
# Signals that an outer process should fail.
# This function should be called by functions that output their value, but wish to fail anyway.
# Such functions should document the need to call checkFail right after their use.
##
function signalFail() {
    touch "${LOCAL_TEST_DIR}/__fail__signal__file"
}

##
# Cleans up the complete environment, everything.
# This function works by executing the commands registered using addCleanupCommand.
##
function cleanup() {
    local cleanStack=( "${CLEANUP_TMP_FILES[@]}" )
    CLEANUP_TMP_FILES=( )
    for index in `seq $(( ${#cleanStack[@]} - 1)) -1 0`; do
        if [ ! -f ${cleanStack[index]} ]; then
            echo "Recursion error in cleanup! Cleanup file at index $index does not exist. Stack trace follows." 1>&2
            local frame=0
            while caller $frame 1>&2; do
                frame=$(($frame + 1))
            done
        else
            # Run each cleanup in a subprocess to make sure that, if any of them fail, the rest continues
            (
                .  ${cleanStack[index]}
            ) &
            wait $!
            rm -f ${cleanStack[index]}
        fi
    done
}

##
# Returns the number of lines currently in the provided cleanup script.
# This function does not work for unknown cleanup file indices and for 0.
#
# This function will also return 0 if `which wc` returns no wc utility. 
# In that case an error is logged as well.
#
# @param    Index of the cleanup file.
#
# @output   The number of lines in the cleanup file specified by the index, or 0.
##
function getCleanupLength() {
    if [ $# -eq 0 ]; then
        echo -n "0"
        return
    fi
    if [ $1 -eq 1 ]; then
        echo -n "0"
        return
    fi
    local index=$1
    if [ -z "${CLEANUP_TMP_FILES[index]}" ]; then
        echo -n "0"
        return
    fi
    if [ ! -f "${CLEANUP_TMP_FILES[index]}" ]; then
        echo -n "0"
        return
    fi
    if [ -z "`which wc 2>/dev/null`" ]; then
        echo "0"
        logError "getCleanupLength called on valid index, but the wc utility wasn't found locally. This probably screwed up some cleanup!" >&2
        return
    fi
    cat "${CLEANUP_TMP_FILES[index]}" | wc -l
}

##
# Adds a command to a cleanup script.
# By default commands are added to the general cleanup script, which will always be executed when cleanup is called.
# One can specify a different cleanup script using a previously obtained index from addCleanupScript as the second argument.
#
# @param    The command line to add to the cleanup script as a single string
# @param    Optional index of the cleanup file
##
function addCleanupCommand() {
    if [ $# -eq 0 ]; then
        return
    fi
    local scriptFile=${CLEANUP_TMP_FILES[0]}

    if [ ! $# -eq 1 ]; then
        # Index given
        index=$2
        scriptFile=${CLEANUP_TMP_FILES[index]}
        if [ ! -e "$scriptFile" ]; then
            logError "Warning: cleanup file index $2 does not exist. Ignoring command. Stack trace follows." 1>&2
            local frame=0
            local trace=""
            while trace=`caller $frame`; do
                frame=$(($frame + 1))
                logError "trace: $trace"
            done
            logError "/trace"
            return
        fi
    fi

    echo "$1" >> "$scriptFile"
}

##
# Inserts a command in a cleanup script.
# This will insert the command at the nth line of cleanup script i, moving all current lines from that line on one down.
# This function does not work for index 0.
#
# The utilities head and tail are required for this function. If they can't be found this function behaves equal to addCleanupCommand
# and an error will be logged.
#
# @param    The command line to insert in the cleanup script as a single string
# @param    The index of the cleanup file (non-zero)
# @param    The line number at which to add the command line (non-negative, smaller than getCleanupLength)
##
function insertCleanupCommand() {
    if [ $# -ne 3 ]; then
        return
    fi
    if [ $2 -eq 0 ]; then
        return
    fi
    local index=$2
    local scriptFile=${CLEANUP_TMP_FILES[index]}
    if [ ! -e "$scriptFile" ]; then
        logError "Warning: cleanup file index $2 does not exist. Ignoring insertion command. Stack trace follow." 1>&2
        local frame=0
        local trace=""
        while trace=`caller $frame`; do
            frame=$(($frame + 1))
            logError "trace: $trace" 1>&2
        done
        logError "/trace" 1>&2
        return
    fi
    if [ -z "`which head`" -o -z "`which tail`" ]; then
        logError "insertCleanupCommand: utilities head and tail not found locally. This probably screwed up some cleanup!" 1>&2
        return
    fi
    local len=`getCleanupLength $2`
    if [ $3 -gt $len ]; then
        logError "insertCleanupCommand: asked to insert cleanup command at line $3, but the file is `getCleanupLength $2` lines long."
        return
    fi
    local tmpFile=`createTempFile`
    cat "$scriptFile" | head -n $3 > "$tmpFile"
    echo "$1" >> "$tmpFile"
    cat "$scriptFile" | tail -n $(($len - $3)) >> "$tmpFile"
    cat "$tmpFile" > "$scriptFile"
    rm "$tmpFile"
}

##
# Adds a new cleanup script to the stack of cleanup scripts.
# Note that cleanup scripts are executed in reverse order of their creation.
# Do not call this function in another thread, such as using backticks: it needs to change the environment of the testing framework.
#
# @return   The index of the new cleanup script for use with addCleanupCommand and removeCleanupScript.
##
function addCleanupScript() {
    local oldIndex=$CLEANUP_MAX_INDEX
    local newIndex=$((CLEANUP_MAX_INDEX + 1))
    CLEANUP_MAX_INDEX=$newIndex
    CLEANUP_TMP_FILES[$newIndex]=`mktemp`
    if [ -z ${CLEANUP_TMP_FILES[newIndex]} ]; then
        CLEANUP_MAX_INDEX=$oldIndex
        logError "Could not create cleanup script file" 1>&2
        fail
    fi
    return $newIndex
}

##
# Removes a previously added cleanup script.
# This means that the script will not be executed on cleanup.
# Note: do not attempt, ever, to remove cleanup scripts with different indices than the ones created by your own calls to addCleanupScript. It would get really messy...
# Do not call this function in another thread, such as using backticks: it needs to change the environment of the testing framework.
#
# @param    The index of the script to remove. Invalid indices (0 included) are ignored.
##
function removeCleanupScript() {
    if [ ! $# -eq 1 ]; then
        return
    fi
    if [ $1 -eq 0 ]; then
        return
    fi
    if [ -z ${CLEANUP_TMP_FILES[$1]} ]; then
        return
    fi
    rm -f ${CLEANUP_TMP_FILES[$1]}
    unset CLEANUP_TMP_FILES[$1]
}
