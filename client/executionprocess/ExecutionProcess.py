#!/usr/bin/env python

# version: 2.010

# File: ExecutionProcess.py ; This file is part of Twister.

# Copyright (C) 2012-2013 , Luxoft

# Authors:
#    Adrian Toader <adtoader@luxoft.com>
#    Andrei Costachi <acostachi@luxoft.com>
#    Andrei Toma <atoma@luxoft.com>
#    Cristian Constantin <crconstantin@luxoft.com>
#    Daniel Cioata <dcioata@luxoft.com>

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Execution Process (EP) should be started as a service, on system startup.
Each EP has a unique name for its user, called Ep Name.
EP gets his status from CE every few seconds. The status can be changed using the Java interface.
When it receives START from CE, it will start the Runner that will execute all test files from suite,
  send all Runner logs to CE and after the execution, it will wait for another START to repeat the cycle.
EP is basically a simple service, designed to start and stop the Runner.
All the hard work is made by the Runner.
"""
from __future__ import print_function
from __future__ import with_statement
from collections import OrderedDict

import os
import sys
import time
import copy
import socket
import signal
import shutil
import binascii
import platform
import threading
import subprocess
import traceback
import tarfile

import rpyc
from plumbum import cli
from rpyc import BgServingThread

# The path when run from ./start_client
TWISTER_PATH = os.getenv('TWISTER_PATH')
# If running portable, find the path of the script
if not TWISTER_PATH:
    TWISTER_PATH, _ = os.path.split(__file__)
# Or the current directory
if not TWISTER_PATH:
    TWISTER_PATH = os.getcwd()
# Make absolute path and strip the eventual slash
TWISTER_PATH = os.path.abspath(TWISTER_PATH).rstrip('/')

os.environ['TWISTER_PATH'] = TWISTER_PATH
if not TWISTER_PATH:
    print('\nTWISTER_PATH environment variable is not set! Exiting!\n')
    exit(1)
else:
    print('\nTWISTER_PATH is set to `{}`.\n'.format(TWISTER_PATH))
sys.path.append(TWISTER_PATH)

EP_CACHE, EP_LOG = None, None

from RunnerClasses import *


# ------------------------------------------------------------------------------

# Test statuses :

STATUS_PENDING  = 10
STATUS_WORKING  = 1
STATUS_PASS     = 2
STATUS_FAIL     = 3
STATUS_SKIPPED  = 4
STATUS_ABORTED  = 5
STATUS_NOT_EXEC = 6
STATUS_TIMEOUT  = 7
STATUS_INVALID  = 8
STATUS_WAITING  = 9

# ------------------------------------------------------------------------------


class Logger(object):

    def __init__(self, stdout, filename):
        self.proxy  = None # To be injected by EP
        self.buffer = ''   # The text buffer
        self.timer  = 0    # Last time the log was sent to CE
        self.stdout = stdout # The OUTPUT stream
        self.logfile = file(filename, 'w')

    def write(self, text):
        """
        Write in the OUT stream, in the log file and send to CE.
        """
        # Write in the OUTPUT stream
        if not self.stdout.closed:
            self.stdout.write(text)
            self.stdout.flush()
        if not self.logfile.closed:
            # Write in the file
            self.logfile.write(text)
        # Send the message to the Central Engine
        self.logLive(text)

    def logLive(self, text):
        """
        If the time is right and the buffer is large enough,
        send the text to the Central Engine.
        """
        global epName
        ctimer = time.time()
        self.buffer += text
        if self.proxy:
            if (ctimer - self.timer) > 1.0:
                self.proxy.logLIVE(epName, binascii.b2a_base64(self.buffer))
                self.buffer = ''
            elif len(self.buffer) > 256:
                self.proxy.logLIVE(epName, binascii.b2a_base64(self.buffer))
                self.buffer = ''
        self.timer = ctimer

    def flush(self):
        """
        Flush all messages the logger.
        """
        global epName
        if not self.stdout.closed:
            self.stdout.flush()
        if not self.logfile.closed:
            self.logfile.flush()
        if self.buffer:
            self.proxy.logLIVE(epName, binascii.b2a_base64(self.buffer))
            self.buffer = ''

    def close(self, *args, **kw):
        """
        Close the logger.
        """
        global epName
        if not self.stdout.closed:
            self.stdout.close()
        if not self.logfile.closed:
            self.logfile.close()
        if self.buffer:
            self.proxy.logLIVE(epName, binascii.b2a_base64(self.buffer))
            self.buffer = ''


class ThreadedLogger(threading.Thread):

    def __init__(self, filename):
        """
        Mock Logger.
        """
        self.proxy  = None # To be injected by EP
        self.buffer = ''   # The text buffer
        self.read_len = 0  # Read file position
        self.timer  = 0    # Last time the log was sent to CE
        self.logfile = filename
        self.exiting = False
        threading.Thread.__init__(self)

    def run(self):
        """
        Watch file changes.
        """
        if not os.path.isfile(self.logfile):
            print('Invalid LOG file: `{}`!\nThe Central Engine will not see the LIVE log for this EP!\n'.format(self.logfile))
            return False
        while not self.exiting:
            data = self.tail()
            self.logLive(data)
            # Wait and retry...
            time.sleep(1)
        # The end
        data = self.tail()
        self.logLive(data, force=True)

    def tail(self):
        """
        Tail on a file.
        """
        f = open(self.logfile, 'rb')
        # Go at "current position"
        f.seek(self.read_len, 0)
        vString = f.read()
        vLen = len(vString)
        # Fix double new-line
        vString = vString.replace('\r\n', '\n')
        vString = vString.replace('\n\r', '\n')
        # Increment "current position"
        self.read_len += vLen
        f.close()
        return vString

    def logLive(self, text, force=False):
        """
        If the time is right and the buffer is large enough,
        send the text to the Central Engine.
        """
        global epName
        ctimer = time.time()
        self.buffer += text
        if self.proxy:
            if (ctimer - self.timer) > 1.0:
                self.proxy.logLIVE(epName, binascii.b2a_base64(self.buffer))
                self.buffer = ''
            elif len(self.buffer) > 256 or force:
                self.proxy.logLIVE(epName, binascii.b2a_base64(self.buffer))
                self.buffer = ''
        self.timer = ctimer

    def write(self, text):
        pass

    def flush(self):
        pass

    def close(self, *args, **kw):
        """
        This will force the thread to exit.
        """
        self.exiting = True


# # #


class TwisterRunner(cli.Application):

    __ce_proxy = None

    userName = cli.SwitchAttr(['-u', '--user'],          str, default='',
               help='The username')
    epName   = cli.SwitchAttr(['-e', '-ep', '--epname'], str, default='',
               help='The Execution Process name')
    cePath   = cli.SwitchAttr(['-s', '--server'],        str, default='',
               help='The Central Engine RPyc IP:Port')
    logFile  = cli.Flag(['-l', '--log'],                 default=False,
               help='Log stdout in a file? Default: DISABLED.')


    @property
    def proxy(self):
        """
        Dinamically connect to the Central Engine.
        """
        # Try to reuse the old connection
        if self.__ce_proxy:
            try:
                self.__ce_proxy.echo('ping')
                return self.__ce_proxy
            except:
                self.__ce_proxy = None
        else:
            self.__ce_proxy = None

        # RPyc config
        config = {
            'allow_pickle': True,
            'allow_getattr': True,
            'allow_setattr': True,
            'allow_delattr': True,
            'allow_all_attrs': True,
            }

        print('EP Debug: Connecting to the Central Engine...')
        ce_ip, ce_port = self.cePath.split(':')

        # If the old connection is broken, connect to the RPyc server
        try:
            p = rpyc.connect(ce_ip, int(ce_port), config=config)
            p.root.hello('ep::{}'.format(self.epName))
        except:
            raise Exception('*ERROR* Cannot connect to CE path `{}`! Exiting!'.format(self.cePath))

        # Authenticate on RPyc server
        try:
            check = p.root.login(self.userName, 'EP')
            bg = BgServingThread(p)
            self.__ce_proxy = p.root
            print('EP Debug: Connected and authenticated to CE at `{}`.\n'.format(self.cePath))
            return self.__ce_proxy
        except:
            raise Exception('*ERROR* Cannot authenticate on CE path `{}`! Exiting!'.format(self.cePath))


    def main(self):

        global epName
        epName = self.epName

        global TWISTER_PATH, EP_CACHE, EP_LOG

        print('~ Start the Execution Process ~')
        print('~ User: {} ; EP: {} ; CE path: {} ~\n'.format(self.userName, self.epName, self.cePath))

        EP_CACHE = TWISTER_PATH + '/.twister_cache/' + self.epName
        EP_LOG = '{}/.twister_cache/{}_LIVE.log'.format(TWISTER_PATH, self.epName)

        try: os.makedirs(EP_CACHE)
        except Exception as e: pass

        if self.logFile:
            self.logger = Logger(sys.stdout, EP_LOG)
            sys.stdout = self.logger
        else:
            self.logger = ThreadedLogger(EP_LOG)
            self.logger.start()

        # All known runners
        self.runners = {
            'tcl': None,
            'python': None,
            'perl': None,
            'java': None,
        }

        # Inject the Central Engine proxy in the logger
        self.logger.proxy = self.proxy

        # Inject all known info about this EP
        ep_host = socket.gethostname()
        try: ep_ip = socket.gethostbyname(ep_host)
        except: ep_ip = ''
        if platform.system().lower() == 'windows':
            system = platform.machine() +' '+ platform.system() +', '+ platform.release()
        else:
            system = platform.machine() +' '+ platform.system() +', '+ ' '.join(platform.linux_distribution())
        self.proxy.setEpVariable(self.epName, 'twister_ep_os', system)
        self.proxy.setEpVariable(self.epName, 'twister_ep_hostname', ep_host)
        self.proxy.setEpVariable(self.epName, 'twister_ep_ip', ep_ip)
        self.proxy.setEpVariable(self.epName, 'twister_ep_python_revision', '.'.join([str(v) for v in sys.version_info]) )

        # The SUT name. Common for all files in this EP.
        self.Sut = self.proxy.getEpVariable(self.epName, 'sut')
        # Get the `exit on test Fail` value
        self.exit_on_test_fail = self.proxy.getUserVariable('exit_on_test_fail')
        # Get tests delay
        self.tc_delay = self.proxy.getUserVariable('tc_delay')

        # After getting Test-Bed name, save all libraries from CE
        self.libs_list = []
        self.saveLibraries()
        # After download, inject libraries path for the current EP
        sys.path.insert(0, '{}/ce_libs'.format(EP_CACHE))

        try:
            import ce_libs
        except Exception as e:
            print('*ERROR* Cannot import the CE libraries! `{}`'.format(e))
            self.exit()
        try:
            from TscCommonLib import TscCommonLib
            self.commonLib = TscCommonLib()
        except Exception as e:
            print('*ERROR* Cannot import the Common libraries! `{}`'.format(e))
            self.exit()

        # Run the tests!
        self.tests()


    def exit(self, timer_f=0.0, *args, **kw):
        """
        Exit safely.
        """
        # Flush all messages
        self.logger.close()
        # Wait a while...
        time.sleep(1)
        print('\n~ Stop the Execution Process ~\n')
        stop = kw.get('stop', True)
        # Send the STOP signal?
        if stop:
            try:
                self.proxy.setEpStatus(self.epName, 0, msg='Execution finished in `{:.2f}` seconds.'.format(timer_f))
            except Exception as e:
                print('Exception on change status: `{}`!'.format(e))
        # ! #
        return


    def saveLibraries(self, libs_list=''):
        """
        Downloads all libraries from Central Engine.
        """

        global TWISTER_PATH, EP_CACHE

        libs_path = '{}/ce_libs'.format(EP_CACHE)
        reset_libs = False

        if not libs_list:
            # This is a list with unique names, sorted alphabetically
            libs_list = self.proxy.listLibraries(False)
            # Pop CommonLib from the list of libraries...
            if 'TscCommonLib.py' in libs_list:
                libs_list.pop(libs_list.index('TscCommonLib.py'))
            # And inject it in the first position! This is important!
            libs_list.insert(0, 'TscCommonLib.py')
            # Save the list for later
            self.libs_list.extend(libs_list)
            reset_libs = True
        else:
            libs_list = [lib.strip() for lib in libs_list.split(';') if lib.strip() not in self.libs_list]
            self.libs_list.extend(libs_list)

        if reset_libs:
            # Remove libs path only if saving libraries for all project
            shutil.rmtree(libs_path, ignore_errors=True)
            # Create the path, after removal
            try: os.makedirs(libs_path)
            except Exception as e: pass

        all_libs = [] # Normal python files or folders
        zip_libs = [] # Zip libraries

        # Create ce_libs library file
        __init = open(libs_path + os.sep + 'ce_libs.py', 'w')
        __init.write('\nimport os, sys\n')
        __init.write('\nPROXY = "{}"\n'.format(self.cePath))
        __init.write('USER = "{}"\n'.format(self.userName))
        __init.write('EP = "{}"\n'.format(self.epName))
        __init.write('SUT = "{}"\n\n'.format(self.Sut))
        __init.close()
        del __init

        for lib in libs_list:
            # Null libraries ?
            if not lib:
                continue
            # Already in the list ?
            if lib in zip_libs or lib in all_libs:
                continue
            if lib.endswith('.zip'):
                zip_libs.append(lib)
            else:
                all_libs.append(lib)

        for lib_file in zip_libs:
            lib_data = self.proxy.downloadLibrary(lib_file)
            time.sleep(0.1) # Must take it slow
            if not lib_data:
                print('ZIP library `{}` does not exist!'.format(lib_file))
                continue

            print('Downloading Zip library `{}` ...'.format(lib_file))

            lib_pth = libs_path + os.sep + lib_file

            f = open(lib_pth, 'wb')
            f.write(lib_data)
            f.close() ; del f

        for lib_file in all_libs:
            lib_data = self.proxy.downloadLibrary(lib_file)
            time.sleep(0.1) # Must take it slow
            if not lib_data:
                print('Library `{}` does not exist!'.format(lib_file))
                continue

            print('Downloading library `{}` ...'.format(lib_file))

            lib_pth = libs_path + os.sep + lib_file

            f = open(lib_pth, 'wb')
            f.write(lib_data)
            f.close() ; del f

            # If the file doesn't have an ext, it's a TGZ library and must be extracted
            if not os.path.splitext(lib_file)[1]:
                # Rename the TGZ
                tgz = lib_pth + '.tgz'
                os.rename(lib_pth, tgz)
                with tarfile.open(tgz, 'r:gz') as binary:
                    os.chdir(libs_path)
                    binary.extractall()

        if reset_libs:
            print('... all libraries downloaded.\n')


    def tests(self):
        """
        Cycle in all files, run each file, in order.
        """
        global logger

        # Count the time
        glob_time = time.time()
        # Shortcut to Central Engine connection
        ce = self.proxy

        if ce.getEpStatus(self.epName) == 'running':
            print('EP Debug: Start to run the tests!')
        else:
            print('EP Debug: EP name `{}` is NOT running! Exiting!\n'.format(self.epName))
            return self.exit(timer_f=0.0, stop=False)

        # Download the Suites Manager structure from Central Engine!
        # This is the initial structure, created from the Project.XML file.
        data = ce.getEpVariable(self.epName, 'suites')
        SuitesManager = copy.deepcopy(data)
        del data

        # Used by all files
        suite_id    = None
        suite_name  = None # Suite name string. This varies for each file.
        suite_files = None # All files from current suite.
        abort_suite = False # Abort suite X, when setup file fails.


        for id, node in SuitesManager.iterNodes():

            # When starting a new suite or sub-suite ...
            # Some files don't belong to this suite, they might belong to the parent of this suite,
            # so each file must update the suite ID!
            if node['type'] == 'suite':

                if not node['children']:
                    print('TC warning: Nothing to do in suite `{}`!\n'.format(suite_str))
                    continue

                suite_id   = id
                suite_name = node['name']
                suite_str  = suite_id +' - '+ suite_name

                # If this is a top level suite, set current_suite flag in EP Variables
                if suite_id in SuitesManager:
                    ce.setEpVariable(self.epName, 'curent_suite', suite_id)

                print('\n===== ===== ===== ===== =====')
                print(' Starting suite `{}`'.format(suite_str))
                print('===== ===== ===== ===== =====\n')

                # Get list of libraries for current suite
                libList = node['libraries']
                if libList:
                    self.saveLibraries(libList)
                    print('')

                # The suite does not execute, so this is the end
                continue


            # Files section
            file_id  = id
            suite_id = node['suite']
            status   = node.get('status', STATUS_INVALID)

            # The name of the file
            filename = node['file']
            # If the file is NOT runnable, download it, but don't execute!
            runnable = node.get('Runnable', 'true')
            # Is this file a setup file?
            setup_file = node.get('setup_file', False)
            # Is this file a teardown file?
            teardown_file = node.get('teardown_file', False)
            # Test-case dependency, if any
            dependancy = node.get('dependancy')
            # Is this test file optional?
            optional_test = node.get('Optional')
            # Configuration files?
            config_files = [c for c in node.get('config_files').split(';') if c]
            # Get args
            args = node.get('param')
            if args:
                args = [p for p in args.split(',') if p]
            else:
                args = []

            # Extra properties, from the applet
            props = dict(node)
            for prop in ['type', 'status', 'file', 'suite', 'dependancy', 'Runnable',
                         'setup_file', 'teardown_file', 'Optional', 'config_files', 'param']:
                # Removing all known File properties
                try: del props[prop]
                except: pass


            print('<<< START filename: `{}:{}` >>>\n'.format(file_id, filename))


            # If a setup file failed, abort the current suite and all sub-suites,
            # unless it's another setup, or teardown file from the current suite!
            # Abort_suite flag is set by the setup files from the beggining of a suite.
            if abort_suite:
                aborted_ids = SuitesManager.getFiles(suite_id=abort_suite, recursive=True)
                current_ids = SuitesManager.getFiles(suite_id=abort_suite, recursive=False)
                if aborted_ids and (file_id in aborted_ids):
                    # If it's a teardown file from current level suite, run it
                    if teardown_file and (file_id in current_ids):
                        print('Running a tear-down file...\n')
                    else:
                        print('EP Debug: Not executed file `{}` because of failed setup file!\n\n'.format(filename))
                        try: ce.setFileStatus(self.epName, file_id, STATUS_NOT_EXEC, 0.0) # File status ABORTED
                        except:
                            trace = traceback.format_exc()[34:].strip()
                            print('Exception on change file status `{}`!\n'.format(trace))
                        print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                        continue
                del aborted_ids, current_ids

            try:
                STATUS = ce.getEpStatus(self.epName)
            except:
                print('Cannot connect to the Central Engine! Exiting!\n')
                return False

            # When a test file is about to be executed and STOP is received, send status ABORTED
            if STATUS == 'stopped':
                try: ce.setFileStatus(self.epName, file_id, STATUS_ABORTED, 0.0) # File status ABORTED
                except:
                    trace = traceback.format_exc()[34:].strip()
                    print('Exception on change file status `{}`!\n'.format(trace))
                print('~ ABORTED: Status STOP, while running! ~')
                diff_time = time.time() - glob_time
                return self.exit(timer_f=diff_time, stop=False)

            # On pause, freeze cycle and wait for Resume or Stop
            elif STATUS == 'paused':
                ce.echo(':: {} is paused!... Must RESUME to continue, or STOP to exit test suite...'.format(self.epName))
                vPauseMsg = False
                while 1:
                    # Print pause message
                    if not vPauseMsg:
                        print('Runner: Execution paused. Waiting for RESUME signal.\n')
                        logger.flush() # Send this message
                        vPauseMsg = True

                    # Wait ...
                    time.sleep(3)
                    try:
                        STATUS = ce.getEpStatus(self.epName)
                    except:
                        print('Cannot connect to the Central Engine! Exiting!\n')
                        return False

                    # On resume, stop waiting
                    if STATUS == 'running' or STATUS == 'resume':
                        ce.echo(':: {} is no longer paused !'.format(self.epName))
                        break
                    # On stop...
                    elif STATUS == 'stopped':
                        # When a test is waiting for resume, but receives STOP, send status NOT EXECUTED
                        try: ce.setFileStatus(self.epName, file_id, STATUS_NOT_EXEC, 0.0)
                        except:
                            trace = traceback.format_exc()[34:].strip()
                            print('Exception on change file status `{}`!\n'.format(trace))
                        print('~ NOT EXECUTED: Status STOP, while waiting for resume ! ~')
                        # Exit the cycle
                        diff_time = time.time() - glob_time
                        return self.exit(timer_f=diff_time, stop=False)


            # If dependency file is PENDING or WORKING, wait for it to finish; for any other status, go next.
            if dependancy and ce.getFileVariable(dependancy, 'status') in [-1, False, STATUS_PENDING, STATUS_WORKING]:
                dep_suite = ce.getFileVariable(dependancy, 'suite')
                dep_file = ce.getFileVariable(dependancy, 'file')

                if dep_file:
                    ce.echo(':: {} is waiting for file `{}::{}` to finish execution...'.format(self.epName, dep_suite, dep_file))
                    try: ce.setFileStatus(self.epName, file_id, STATUS_WAITING, 0.0) # Status WAITING
                    except:
                        trace = traceback.format_exc()[34:].strip()
                        print('Exception on change file status `{}`!\n'.format(trace))

                    while 1:
                        time.sleep(3)
                        # Reload info about dependency file
                        if  ce.getFileVariable(dependancy, 'status') not in [-1, False, STATUS_PENDING, STATUS_WORKING]:
                            ce.echo(':: {} is not longer waiting for dependency!'.format(self.epName))
                            break

                del dep_suite, dep_file


            # Download file from Central Engine!
            str_to_execute = ce.downloadFile(self.epName, file_id)

            # If CE sent False, it means the file is empty, does not exist, or it's not runnable.
            if str_to_execute == '':
                print('EP Debug: File path `{}` does not exist!\n'.format(filename))
                if setup_file:
                    abort_suite = suite_id
                    print('*ERROR* Setup file for suite `{}` cannot run! No such file! All suite will be ABORTED!\n\n'.format(suite_name))
                try: ce.setFileStatus(self.epName, file_id, STATUS_SKIPPED, 0.0) # Status SKIPPED
                except:
                    trace = traceback.format_exc()[34:].strip()
                    print('Exception on change file status `{}`!\n'.format(trace))
                print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                continue

            elif not str_to_execute:
                print('EP Debug: File `{}` will be skipped.\n'.format(filename))
                # Skipped setup files are ok, no need to abort.
                try: ce.setFileStatus(self.epName, file_id, STATUS_SKIPPED, 0.0) # Status SKIPPED
                except:
                    trace = traceback.format_exc()[34:].strip()
                    print('Exception on change file status `{}`!\n'.format(trace))
                print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                continue

            # Don' Run NON-runnable files, but Download them!
            if runnable.lower() != 'true':
                print('File `{}` is not runnable, it will be downloaded, but not executed.\n'.format(filename))
                fpath = EP_CACHE +os.sep+ os.path.split(filename)[1]
                f = open(fpath, 'wb')
                f.write(str_to_execute.data)
                f.close() ; del f
                try: ce.setFileStatus(self.epName, file_id, STATUS_SKIPPED, 0.0) # Status SKIPPED
                except:
                    trace = traceback.format_exc()[34:].strip()
                    print('Exception on change file status `{}`!\n'.format(trace))
                print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                continue


            file_ext = os.path.splitext(filename)[1].lower()

            # If file type is TCL
            if file_ext in ['.tcl']:
                if not self.runners['tcl']:
                    self.runners['tcl'] = TCRunTcl()
                current_runner = self.runners['tcl']

            # If file type is PERL
            elif file_ext in ['.plx']:
                if not self.runners['perl']:
                    self.runners['perl'] = TCRunPerl()
                current_runner = self.runners['perl']

            # If file type is PYTHON
            elif file_ext in ['.py', '.pyc', '.pyo']:
                if not self.runners['python']:
                    self.runners['python'] = TCRunPython()
                current_runner = self.runners['python']

            # If file type is JAVA
            elif file_ext in ['.java']:
                if not self.runners['java']:
                    self.runners['java'] = TCRunJava()
                current_runner = self.runners['java']

            # Unknown file type
            else:
                print('TC warning: Extension type `{}` is unknown and will be ignored!'.format(file_ext))
                if setup_file:
                    abort_suite = suite_id
                    print('*ERROR* Setup file for suite `{}` cannot run! Unknown extension file! All suite will be ABORTED!\n\n'.format(suite_name))
                try: ce.setFileStatus(self.epName, file_id, STATUS_NOT_EXEC, 0.0) # Status NOT_EXEC
                except:
                    trace = traceback.format_exc()[34:].strip()
                    print('Exception on change file status `{}`!\n'.format(trace))
                print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                continue


            # If there is a delay between tests, wait here
            if self.tc_delay:
                print('EP Debug: Waiting {} seconds before starting the test...\n'.format(self.tc_delay))
                time.sleep(self.tc_delay)


            # Check the general status again...
            try:
                if ce.getEpStatus(self.epName) == 'stopped':
                    # Exit the cycle
                    diff_time = time.time() - glob_time
                    return self.exit(timer_f=diff_time, stop=False)
            except:
                print('Cannot connect to the Central Engine! Exiting!\n')
                return False

            # The file is preparing
            try: ce.setFileStatus(self.epName, file_id, STATUS_WORKING, 0.0) # Status WORKING
            except:
                trace = traceback.format_exc()[34:].strip()
                print('Exception on change file status `{}`!\n'.format(trace))

            # Start counting test time
            timer_i = time.time()
            start_time = time.strftime('%Y-%m-%d %H:%M:%S')

            result = None

            # --------------------------------------------------------------------------------------
            # RUN CURRENT TEST!

            globs = {
                'USER'      : self.userName,
                'EP'        : self.epName,
                'SUT'       : self.Sut,
                'SUITE_ID'  : suite_id,
                'SUITE_NAME': suite_name,
                'FILE_ID'   : file_id,
                'FILE_NAME' : filename,
                'PROPERTIES': props,
                'CONFIG'    : config_files,
                'PROXY'     : ce
            }

            # Find all functions from commonLib
            to_inject = [ f for f in dir(self.commonLib) if callable(getattr(self.commonLib, f)) ]
            # Expose all known function in tests
            for f in to_inject:
                # print('DEBUG: Exposing Python command `{}` into TCL...'.format(f))
                globs[f] = getattr(self.commonLib, f)

            try:
                result = current_runner._eval(str_to_execute, globs, args)
                result = str(result).upper()
                print('\n>>> File `{}` returned `{}`. <<<\n'.format(filename, result))

            except Exception as e:
                # On error, print the error message, but don't exit
                print('\nTest case exception:')
                print(traceback.format_exc()[34:].strip())
                print('\n>>> File `{}` execution CRASHED. <<<\n'.format(filename))

                ce.echo('*ERROR* Error executing file `{}`!'.format(filename))
                try: ce.setFileStatus(self.epName, file_id, STATUS_FAIL, (time.time() - timer_i))
                except:
                    trace = traceback.format_exc()[34:].strip()
                    print('Exception on change file status `{}`!\n'.format(trace))

                # If status is FAIL and the file is not Optional and Exit on test fail is ON, CLOSE the runner
                if not optional_test and self.exit_on_test_fail:
                    print('*ERROR* Mandatory file `{}` returned FAIL! Closing the runner!\n\n'.format(filename))
                    ce.echo('*ERROR* Mandatory file `{}::{}::{}` returned FAIL! Closing the runner!'\
                        ''.format(self.epName, suite_name, filename))
                    print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                    # Exit the cycle
                    diff_time = time.time() - glob_time
                    return self.exit(timer_f=diff_time)

                # If status is FAIL, and the file is a setup file, CANCEL all suite
                if setup_file:
                    abort_suite = suite_id
                    print('*ERROR* Setup file for suite `{}` returned FAIL! All suite will be ABORTED!\n\n'.format(suite_name))
                    ce.echo('*ERROR* Setup file for `{}::{}` returned FAIL! All suite will be ABORTED!'\
                        ''.format(self.epName, suite_name))

                # Send crash detected = True
                ce.setFileVariable(self.epName, file_id, 'twister_tc_crash_detected', 1)
                # Stop counting time. END OF TEST!
                timer_f = time.time() - timer_i
                end_time = time.strftime('%Y-%m-%d %H:%M:%S')
                print('Test statistics: Start time {} -- End time {} -- {:0.2f} sec.\n'.format(start_time, end_time, timer_f))
                print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                # Skip this cycle, go to next file
                continue

            # Stop counting time. END OF TEST!
            timer_f = time.time() - timer_i
            end_time = time.strftime('%Y-%m-%d %H:%M:%S')
            # --------------------------------------------------------------------------------------

            print('Test statistics: Start time {} -- End time {} -- {:0.2f} sec.\n'.format(start_time, end_time, timer_f))


            try:
                if result==STATUS_PASS or result == 'PASS':
                    ce.setFileStatus(self.epName, file_id, STATUS_PASS, timer_f) # File status PASS
                elif result==STATUS_SKIPPED or result in ['SKIP', 'SKIPPED']:
                    ce.setFileStatus(self.epName, file_id, STATUS_SKIPPED, timer_f) # File status SKIPPED
                elif result==STATUS_ABORTED or result in ['ABORT', 'ABORTED']:
                    ce.setFileStatus(self.epName, file_id, STATUS_ABORTED, timer_f) # File status ABORTED
                elif result==STATUS_NOT_EXEC or result in ['NOT-EXEC', 'NOT EXEC', 'NOT EXECUTED']:
                    ce.setFileStatus(self.epName, file_id, STATUS_NOT_EXEC, timer_f) # File status NOT_EXEC
                elif result==STATUS_TIMEOUT or result == 'TIMEOUT':
                    ce.setFileStatus(self.epName, file_id, STATUS_TIMEOUT, timer_f) # File status TIMEOUT
                elif result==STATUS_INVALID or result == 'INVALID':
                    ce.setFileStatus(self.epName, file_id, STATUS_INVALID, timer_f) # File status INVALID
                else:
                    result = STATUS_FAIL
                    ce.setFileStatus(self.epName, file_id, STATUS_FAIL, timer_f) # File status FAIL
            except:
                trace = traceback.format_exc()[34:].strip()
                print('EXCEPTION on final change file status `{}`!'.format(trace))


            # If status is not PASS
            if (result!=STATUS_PASS or result!='PASS'):

                # If status is FAIL and the file is not Optional and Exit on test fail is ON, CLOSE the runner
                if not optional_test and self.exit_on_test_fail:
                    print('*ERROR* Mandatory file `{}` returned FAIL! Closing the runner!\n\n'.format(filename))
                    ce.echo('*ERROR* Mandatory file `{}::{}::{}` returned FAIL! Closing the runner!'\
                        ''.format(self.epName, suite_name, filename))
                    print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))
                    # Exit the cycle
                    diff_time = time.time() - glob_time
                    return self.exit(timer_f=diff_time)

                if setup_file:
                    # If the file is a setup file, CANCEL all suite
                    abort_suite = suite_id
                    print('*ERROR* Setup file for suite `{}` did not PASS! All suite will be ABORTED!\n\n'.format(suite_name))
                    ce.echo('*ERROR* Setup file for `{}::{}` returned FAIL! All suite will be ABORTED!'\
                        ''.format(self.epName, suite_name))


            print('<<< END filename: `{}:{}` >>>\n'.format(file_id, filename))

            #---------------------------------------------------------------------------------------

        print('\n==========================')
        print('. . . All tests done . . .')
        print('==========================\n')


        del SuitesManager

        # Print the final message
        diff_time = time.time() - glob_time
        return self.exit(timer_f=diff_time)


# # #


if __name__=='__main__':

    if len(sys.argv) < 3:
        print('*ERROR* EP must start with 3 parameters!')
        print(' usage : python ExecutionProcess.py Ep_Name Host:Port')
        exit(1)


    # If this scripts is running Portable from twister/client/exec ...
    path = TWISTER_PATH
    path_exploded = []

    while 1:
        path, folder = os.path.split(path)
        if folder:
            path_exploded.append(folder)
        else:
            if path:
                path_exploded.append(path.rstrip(os.sep))
            break

    path_exploded.reverse()

    # Then append ths Twister Path to Python path
    if path_exploded[-1] == 'executionprocess':
        path_exploded = path_exploded[:-2]
        path = os.sep.join(path_exploded)
        print('Portable mode: Appending `{}` to python path.\n'.format(path))
        sys.path.append(path)


    signal.signal(signal.SIGTERM, exit)
    signal.signal(signal.SIGINT, exit)

    epName = None # Used by the logger when sending the Live Log

    TwisterRunner.run()

# Eof()
