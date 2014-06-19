
# File: CeFs.py ; This file is part of Twister.

# version: 3.015

# Copyright (C) 2012-2014, Luxoft

# Authors:
#    Andrei Costachi <acostachi@luxoft.com>
#    Cristi Constantin <crconstantin@luxoft.com>
#    Daniel Cioata <dcioata@luxoft.com>
#    Mihai Tudoran <mtudoran@luxoft.com>

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
Local file system; used to help workign with files where user is owner
"""

import os, sys
import time
import copy
import random
import socket
import subprocess
from plumbum import local
import rpyc

socket.setdefaulttimeout(3)
from thread import allocate_lock

TWISTER_PATH = os.getenv('TWISTER_PATH')
if not TWISTER_PATH:
    print('$TWISTER_PATH environment variable is not set! Exiting!')
    exit(1)
if TWISTER_PATH not in sys.path:
    sys.path.append(TWISTER_PATH)

from common.helpers    import *
from common.tsclogging import *

#
__all__ = ['LocalFS']
#


def singleton(cls):
    """ Lauch single instance"""
    instances = {}
    def getinstance(*args, **kwargs):
        """ Return new/existing instance """
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]
    return getinstance


@singleton
class LocalFS(object):
    """
    All local file operations should be done via THIS class.
    This is a singleton.
    """
    _services = {}
    _srv_lock = allocate_lock()


    def __init__(self, project=None):
        if os.getuid():
            logError('Local FS: Central Engine must run as ROOT in order to start the User Service!')
        self.project = project
        logInfo('Created Local FS.')


    def _kill(self, user):
        """ Stop service """
        ps   = local['ps']
        grep = local['grep']

        try:
            pids = (ps['aux'] | grep['/server/UserService.py'] | grep['^' + user] | grep['FS'])()
        except Exception:
            return

        # Kill all leftover processes
        for line in pids.strip().splitlines():
            li = line.strip().decode('utf').split()
            PID = int(li[1])
            del li[2:5]
            if '/bin/sh' in li:
                continue
            if '/bin/grep' in li:
                continue
            logDebug('User {}: Killing ugly zombie `{}`.'.format(user, ' '.join(li)))
            try:
                os.kill(PID, 9)
            except:
                pass


    def _usr_service(self, user, op='read'):
        """
        Launch a user service.
        """
        if op not in ['read', 'write']:
            logWarning('Invalid FS operation `{}`, for user `{}`! Will reset to "read".'.format(op, user))
            op = 'read'

        # Must block here, so more users cannot launch Logs at the same time and lose the PID
        with self._srv_lock:

            # Try to re-use the logger server, if available
            conn = self._services.get(user, {}).get('conn_' + op, None)
            if conn:
                try:
                    conn.ping(data='Hello', timeout=30.0)
                    #logDebug('Reuse old {} User Service connection for `{}` OK.'.format(op, user))
                    return conn
                except Exception as e:
                    logWarning('Cannot reuse {} User Service for `{}`: `{}`.'.format(op, user, e))
                    self._kill(user)
            else:
                logInfo('Launching a User Service for `{}`, the first time...'.format(user))

            port = None

            # If the server is not available, search for a free port in the safe range...
            while 1:
                port = random.randrange(63000, 65000)
                try:
                    socket.create_connection((None, port), 1)
                except:
                    break

            p_cmd = 'su {} -c "{} -u {}/server/UserService.py {} FS"'.format(user, sys.executable, TWISTER_PATH, port)
            proc = subprocess.Popen(p_cmd, cwd='{}/twister'.format(userHome(user)), shell=True,
                   close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.poll()
            time.sleep(2.0)

            config = {
                'allow_pickle': True,
                'allow_getattr': True,
                'allow_setattr': True,
                'allow_delattr': True
            }

            retry = 10
            delay = 0.5
            success = False

            while retry > 0:
                if success:
                    break

                try:
                    stream_r = rpyc.SocketStream.connect('127.0.0.1', port, timeout=5.0)
                    conn_read = rpyc.connect_stream(stream_r, config=config)
                    conn_read.root.hello()
                    logDebug('Connected to User Service for `{}`.'.format(user))
                    success = True
                except Exception as e:
                    logWarning('Cannot connect to User Service for `{}` - Exception: `{}`! '
                            'Wait {}s...'.format(user, e, delay))

                if success:
                    try:
                        stream_w = rpyc.SocketStream.connect('127.0.0.1', port, timeout=5.0)
                        conn_write = rpyc.connect_stream(stream_w, config=config)
                        conn_write.root.hello()
                        break
                    except Exception as e:
                        logWarning('Cannot connect to User Service for `{}` - Exception: `{}`! '
                                'Wait {}s...'.format(user, e, delay))
                        success = False

                time.sleep(delay)
                retry -= 1
                delay += 0.75

            if not success:
                logError('Error on starting User Service for `{}`!'.format(user))
                return None

            # Save the process inside the block.  99% of the time, this block is executed instantly!
            self._services[user] = {'proc': proc, 'conn_read': conn_read, 'conn_write': conn_write, 'port': port}

        logDebug('User Service for `{}` launched on `127.0.0.1:{}` - PID `{}`.'.format(user, port, proc.pid))

        return self._services[user].get('conn_' + op, None)


    # ----- USER ---------------------------------------------------------------


    def file_size(self, user, fpath):
        """
        Get file size for 1 file. Client access via RPyc.
        """
        if not fpath:
            return False
        srvr = self._usr_service(user)
        if srvr:
            try:
                return srvr.root.file_size(fpath)
            except Exception:
                return -1
        else:
            return -1


    def read_user_file(self, user, fpath, flag='r', fstart=0):
        """
        Read 1 file. Client access via RPyc.
        """
        if not fpath:
            return False
        srvr = self._usr_service(user)
        if srvr:
            try:
                return srvr.root.read_file(fpath, flag, fstart)
            except Exception as e:
                err = '*ERROR* Cannot read file `{}`, user `{}`! {}'.format(fpath, user, e)
                logWarning(err)
                return err
        else:
            return False


    def write_user_file(self, user, fpath, fdata, flag='w'):
        """
        Read 1 file. Client access via RPyc.
        """
        if not fpath:
            return False
        srvr = self._usr_service(user, 'write')
        if len(fdata) > 20*1000*1000:
            err = '*ERROR* File data too long `{}`: {}; User {}.'.format(fpath, len(fdata), user)
            logWarning(err)
            return err
        if srvr:
            try:
                return srvr.root.write_file(fpath, fdata, flag)
            except Exception as e:
                err = '*ERROR* Cannot write into file `{}`, user `{}`! {}'.format(fpath, user, e)
                logWarning(err)
                return err
        else:
            return False


    def copy_user_file(self, user, fpath, newpath):
        """ copy an user file """
        if not fpath:
            return False
        srvr = self._usr_service(user, 'write')
        if srvr:
            return srvr.root.copy_file(fpath, newpath)
        else:
            return False


    def move_user_file(self, user, fpath, newpath):
        """ move/rename a user file """
        if not fpath:
            return False
        srvr = self._usr_service(user, 'write')
        if srvr:
            return srvr.root.move_file(fpath, newpath)
        else:
            return False


    def delete_user_file(self, user, fpath):
        """ delete user file """
        if not fpath:
            return False
        srvr = self._usr_service(user)
        if srvr:
            return srvr.root.delete_file(fpath)
        else:
            return False


    def create_user_folder(self, user, fdir):
        """ create a folder in user client directory """
        if not fdir:
            return False
        srvr = self._usr_service(user)
        if srvr:
            return srvr.root.create_folder(fdir)
        else:
            return False


    def list_user_files(self, user, fdir, hidden=True, recursive=True, filter=[]):
        """ list the files in user directory """
        if not fdir:
            return False

        srvr = self._usr_service(user)
        if srvr:
            try:
                files = srvr.root.list_files(fdir, hidden, recursive, filter)
                return copy.copy(files)
            except Exception as e:
                err = '*ERROR* Cannot list files `{}`, user `{}`! {}'.format(fdir, user, e)
                logWarning(err)
                return err
        else:
            return False


    def delete_user_folder(self, user, fdir):
        """ Delete user folder """
        if not fdir:
            return False
        srvr = self._usr_service(user)
        if srvr:
            return srvr.root.delete_folder(fdir)
        else:
            return False


    def targz_user_folder(self, user, fdir):
        """ Archive a folder """
        if not fdir:
            return False
        srvr = self._usr_service(user)
        if srvr:
            return srvr.root.targz_folder(fdir)
        else:
            return False


    # ----- SYSTEM -------------------------------------------------------------


    @staticmethod
    def sys_file_size(fpath):
        """
        Get file size for 1 file. ROOT access.
        """
        if not fpath:
            return False
        try:
            fsize = os.stat(fpath).st_size
            # logDebug('File `{}` is size `{}`.'.format(fpath, fsize))
            return fsize
        except Exception as e:
            err = '*ERROR* Cannot find file `{}`! {}'.format(fpath, e)
            logWarning(err)
            return err


    @staticmethod
    def read_system_file(fpath, flag='r', fstart=0):
        """
        Read 1 file. ROOT access.
        """
        if not fpath:
            return False
        if flag not in ['r', 'rb']:
            err = '*ERROR* Invalid flag `{}`! Cannot read!'.format(flag)
            logWarning(err)
            return err
        if not os.path.isfile(fpath):
            err = '*ERROR* No such file `{}`!'.format(fpath)
            logWarning(err)
            return err
        try:
            with open(fpath, flag) as f:
                # logDebug('Reading file `{}`, flag `{}`.'.format(fpath, flag))
                if fstart:
                    f.seek(fstart)
                fdata = f.read()
                if len(fdata) > 20*1000*1000:
                    err = '*ERROR* File data too long `{}`: {}!'.format(fpath, len(fdata))
                    logWarning(err)
                    return err
                return fdata
        except Exception as e:
            err = '*ERROR* Cannot read file `{}`! {}'.format(fpath, e)
            logWarning(err)
            return err


    @staticmethod
    def write_system_file(fpath, fdata, flag='a'):
        """
        Write data in a file. ROOT access.
        Overwrite or append, ascii or binary.
        """
        if not fpath:
            return False
        if flag not in ['w', 'wb', 'a', 'ab']:
            err = '*ERROR* Invalid flag `{}`! Cannot read!'.format(flag)
            logWarning(err)
            return err
        try:
            with open(fpath, flag) as f:
                f.write(fdata)
            # if flag == 'w':
            #     logDebug('Written `{}` chars in ascii file `{}`.'.format(len(fdata), fpath))
            # elif flag == 'wb':
            #     logDebug('Written `{}` chars in binary file `{}`.'.format(len(fdata), fpath))
            # elif flag == 'a':
            #     logDebug('Appended `{}` chars in ascii file `{}`.'.format(len(fdata), fpath))
            # else:
            #     logDebug('Appended `{}` chars in binary file `{}`.'.format(len(fdata), fpath))
            return True
        except Exception as e:
            err = '*ERROR* Cannot write into file `{}`! {}'.format(fpath, e)
            logWarning(err)
            return err


    def delete_system_file(self, fname):
        """ Dummy method """
        pass


    def create_system_folder(self, fdir):
        """ Dummy method """
        pass


    def list_system_files(self, fdir):
        """ Dummy method """
        pass


    def delete_system_folder(self, fdir):
        """ Dummy method """
        pass

#

if __name__ == '__main__':

    FS_1 = LocalFS()
    FS_2 = LocalFS()

    assert FS_1 == FS_2, 'Not equal!'
    assert FS_1 is FS_2, 'Not identical!'

    print(FS_1)
    print(FS_2)
    print('Ok.')


# Eof()
