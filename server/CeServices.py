
# File: CeServices.py ; This file is part of Twister.

# version: 3.001

# Copyright (C) 2012-2013 , Luxoft

# Authors:
#    Adrian Toader <adtoader@luxoft.com>
#    Andrei Costachi <acostachi@luxoft.com>
#    Andrei Toma <atoma@luxoft.com>
#    Cristi Constantin <crconstantin@luxoft.com>
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
User Service Module
"""
import os, sys
import json
import time
import signal
import subprocess
import binascii

SM_LIST       = 0
SM_START      = 1
SM_STOP       = 2
SM_STATUS     = 3
SM_DESCRIP    = 4
SM_GET_CONFIG = 5
SM_SET_CONFIG = 6
SM_GET_LOG    = 7

SM_COMMAND_MAP = {
    SM_START      : 'start',
    SM_STOP       : 'stop',
    SM_STATUS     : 'status',
    SM_DESCRIP    : 'description',
    SM_GET_CONFIG : 'get config',
    SM_SET_CONFIG : 'set config',
    SM_GET_LOG    : 'get log',
    SM_LIST       : 'list'
}

TWISTER_PATH = os.getenv('TWISTER_PATH')
if not TWISTER_PATH:
    print('$TWISTER_PATH environment variable is not set! Exiting!')
    exit(1)
if TWISTER_PATH not in sys.path:
    sys.path.append(TWISTER_PATH)

from common.tsclogging import *
from common import iniparser

#

class ServiceManager(object):
    """ manager for user service """

    def __init__(self):

        logDebug('SM: Starting Service Manager...')

        self.twister_services = []
        cfg_path = '{0}/config/services.ini'.format(TWISTER_PATH)
        cfg = iniparser.ConfigObj(cfg_path)

        for service in cfg:
            if service == 'DEFAULT':
                continue
            cfg[service]['name'] = service
            self.twister_services.append(cfg[service])

        logDebug('SM: Found `{0}` services: `{1}`.'.format(len(self.twister_services), ', '.join(cfg.keys())))
        del cfg, cfg_path


    def __del__(self):

        logDebug('SM: Stopping Service Manager...')

        for service in self.twister_services:
            if self.service_status(service) == -1:
                self.service_stop(service)

        del self.twister_services


    def send_command(self, command, name='', *args, **kwargs):
        """ send command to service """
        logFull('CeServices:send_command')

        if command == SM_LIST or command == SM_COMMAND_MAP[SM_LIST]:
            return self.list_services()

        found = False

        service = None
        for service_item in self.twister_services:
            if name == service_item['name']:
                found = True
                service = service_item
                break

        if not found:
            logDebug('SM: Invalid service name: `%s`!'.format(name))
            return False

        elif command == SM_STATUS or command == SM_COMMAND_MAP[SM_STATUS]:
            return self.service_status(service)

        elif command == SM_DESCRIP or command == SM_COMMAND_MAP[SM_DESCRIP]:
            return service.get('descrip')

        if command == SM_START or command == SM_COMMAND_MAP[SM_START]:
            return self.service_start(service)

        elif command == SM_STOP or command == SM_COMMAND_MAP[SM_STOP]:
            return self.service_stop(service)

        elif command == SM_GET_CONFIG or command == SM_COMMAND_MAP[SM_GET_CONFIG]:
            return self.read_config(service)

        elif command == SM_SET_CONFIG or command == SM_COMMAND_MAP[SM_SET_CONFIG]:
            try:
                return self.save_config(service, args[0][0])
            except:
                return 'SM: Invalid number of parameters for save config!'

        elif command == SM_GET_LOG or command == SM_COMMAND_MAP[SM_GET_LOG]:
            try:
                return self.get_console_log(service, read=args[0][0], fstart=args[0][1])
            except:
                return 'SM: Invalid number of parameters for read log!'

        else:
            return 'SM: Unknown command number: `{0}`!'.format(command)


    def list_services(self):
        """ return list of services """
        logFull('CeServices:list_services')
        srv = []
        for service in self.twister_services:
            srv.append(service['name'])
        return ','.join(srv)


    def service_status(self, service):
        """ return status of service """
        logFull('CeServices:service_status')
        # Values are: -1, 0, or any error code
        # -1 means the app is still running

        tprocess = service.get('pid', 0)
        rc = 0

        if tprocess:
            tprocess.poll()
            rc = tprocess.returncode

        if rc is None:
            rc = -1

        return rc


    def service_start(self, service):
        """ start service """
        logFull('CeServices:service_start')

        tprocess = service.get('pid', 0)

        if tprocess:
            # Check if child process has terminated
            tprocess.poll()

            if tprocess.returncode is None:
                logDebug('SM: Service name `{}` is already running with PID `{}`.'.format(
                    service['name'], tprocess.pid))
                return True

        del tprocess

        script_path = '{}/services/{}/{}'.format(TWISTER_PATH, service['name'], service['script'])

        if service['config']:
            config_path = '{}/services/{}/{}'.format(TWISTER_PATH, service['name'], service['config'])
        else:
            config_path = ''

        if not os.path.isfile(script_path):
            error = 'SM: Cannot start service `{}`! No such script file `{}`!'.format(
                service['name'], script_path)
            logError(error)
            return error

        if service['config'] and (not config_path):
            error = 'SM: Cannot start service `{}`! No such config file `{}`!'.format(
                service['name'], config_path)
            logError(error)
            return error

        service['pid'] = 0 # Delete process here
        env = os.environ
        env.update({'TWISTER_PATH': TWISTER_PATH})

        log_path = '{}/services/{}/{}'.format(TWISTER_PATH, service['name'], service['logfile'])
        logs_dir = os.path.split(log_path)[0]

        if not os.path.isdir(logs_dir):
            try:
                os.mkdir(logs_dir)
            except:
                logError('SM: Cannot create logs folder `{}`!'.format(logs_dir))

        p_cmd = [sys.executable, '-u', script_path, config_path]

        with open(log_path, 'wb') as out:
            try:
                tprocess = subprocess.Popen(p_cmd, stdout=out, stderr=out, env=env)
            except Exception as e:
                error = 'SM: Cannot start service `{}` with config file `{}`!\n'\
                    'Exception: `{}`!'.format(service['name'], config_path, e)
                logError(error)
                return error

        service['pid'] = tprocess
        logDebug('Started service `{}`, using script `{}` and config `{}`, with PID `{}`.'.format(
            service['name'], script_path, config_path, tprocess.pid))
        return True


    def service_stop(self, service):
        """ stop service """
        logFull('CeServices:service_stop')

        rc = self.service_status(service)
        if not rc:
            logDebug('SM: Service name `{}` is not running.'.format(service['name']))
            return False

        tprocess = service.get('pid', 0)

        if isinstance(tprocess, int):
            logError('SM: Cannot stop service `{}`!'.format(service['name']))

        try:
            tprocess.terminate()
        except Exception as e:
            logError('SM: Cannot stop service: `{}`, exception `{}`!'.format(service['name'], e))
            return False

        try:
            time.sleep(0.1)
            os.killpg(tprocess.pid, signal.SIGTERM)
            time.sleep(0.1)
            tprocess.kill()
        except:
            pass

        logWarning('SM: Stopped service: `{}`.'.format(service['name']))
        return True


    def service_kill(self, service):
        """ forced stop user service """
        logFull('CeServices:service_kill')

        return self.service_stop(service)


    def read_config(self, service):
        """ Read configuration """
        logFull('CeServices:read_config')

        config_path = '{0}/services/{1}/{2}'.format(TWISTER_PATH, service['name'], service['config'])

        if not os.path.isfile(config_path):
            logError('SM: No such config file `{0}`!'.format(config_path))
            return False

        with open(config_path, 'rb') as out:
            data = out.read()

        return data or ''


    def save_config(self, service, data):
        """ Save configuration """
        logFull('CeServices:save_config')

        config_path = '{0}/services/{1}/{2}'.format(TWISTER_PATH, service['name'], service['config'])

        if not os.path.isfile(config_path):
            logError('SM: No such config file `{0}`!'.format(config_path))
            return False

        with open(config_path, 'wb') as out:
            out.write(data)

        return True


    def get_console_log(self, service, read, fstart):
        """
        Called in the Java GUI to show the logs.
        """
        logFull('CeServices:get_console_log')
        if fstart is None:
            return '*ERROR for {0}!* Parameter FSTART is NULL!'.format(service['name'])

        filename = '{0}/services/{1}/{2}'.format(TWISTER_PATH, service['name'], service['logfile'])

        if not os.path.exists(filename):
            return '*ERROR for {0}!* No such log file `{0}`!'.format(service['name'], filename)

        if not read or read == '0':
            return os.path.getsize(filename)

        fstart = long(fstart)
        f = open(filename)
        f.seek(fstart)
        data = f.read()
        f.close()

        return binascii.b2a_base64(data)


# Eof()
