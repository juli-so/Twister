import os
import sys
import ast
import copy
import thread
import errno

try: import simplejson as json
except: import json

import cherrypy
from lxml import etree
from binascii import hexlify
from cherrypy import _cptools
from mako.template import Template
import time

TWISTER_PATH = os.getenv('TWISTER_PATH')
if not TWISTER_PATH:
    print('TWISTER_PATH environment variable is not set! Exiting!')
    exit(1)
if TWISTER_PATH not in sys.path:
    sys.path.append(TWISTER_PATH)

from common.tsclogging import *
from common.helpers    import *


RESOURCE_FREE     = 1
RESOURCE_BUSY     = 2
RESOURCE_RESERVED = 3

class CommonAllocator(object):


    def user_info(self, props={}):
        '''
        This method returns info about user. The list
        returned: [user_name, user_roles]
        '''
        logFull('CeCommonAllocator:user_info')
        # Check the username from CherryPy connection
        try: user = cherrypy.session.get('username')
        except: user = ''
        # Fallback
        if not user:
            user = props.get('__user', '')

        user_roles = self.project.authenticate(user)
        default = {'user': user, 'roles': [], 'groups': []}
        if not user_roles: return default
        user_roles.update({'user': user})
        return [user_roles.get('user'), user_roles]


    def get_id(self, node_id, resource):
        '''
        This method searches in resource for node_id and
        returns its dictionary. Also, in the path key
        we save the complete path from root to this node_id.
        '''

        logFull("CeCommonAllocator: get_id: Getting the id {}".format(node_id))

        if not resource:
            return None

        if resource.get('id') == node_id:
            return resource

        if not resource.get('children'):
            return None

        for node in resource.get('children'):
            result = self.get_id( node_id, resource['children'][node])
            if result:
                return result


    def get_path(self, query, resource):
        '''
        This method searches in resource for a path (query) and
        returns its dictionary. Also, in the path key
        we save the absolute path (from root)
        '''

        logDebug("CeCommonAllocator: get_path: Getting the path {}".format(query))

        parts = [q for q in query.split('/') if q]
        for part in parts:
            if not resource:
                logDebug('*ERROR* we did not find a result for this query: {}!'.format(query))
                return None

            if part in resource['children']:
                resource = resource['children'].get(part)
            else:
                logDebug('*ERROR* we did not find a result for this path: {}!'.format(part))
                return None

        return resource


    def change_path(self, result, path):
        '''
        update the path of all kids if the result is renamed for example
        '''

        logDebug("CeCommonAllocator: change_path  {} ".format(path))
        if not result:
            return False
        if not result.get('children'):
            return True

        for node in result.get('children'):
            result['children'][node]['path'] = path + [node]
            self.change_path(result['children'][node], result['children'][node]['path'])

        if not result:
            return True


    def get_resource(self, query, resource = None):
        '''
        Get the resource from resource by path or id.
        We can search for query in self.resoruces or
        in any other resource given as parameter
        '''

        logDebug("CeCommonAllocator: get_resource: Getting the resource {}".format(query))

        if not resource:
            resource = self.resources

        if not resource or not query:
            msg = '{} -- {}\nCannot get a null resource !'.format(resource, query)
            logError(msg)
            return None

        if ':' in query:
            query = query.split(':')[0]

        # If the query is an ID
        if '/' not in query:
            result = self.get_id(query, resource)
        else:
            result = self.get_path(query, resource)

        if result:
                return dict(result)

        return result


    def valid_props(self, props):
        '''
        Verify if we have recevied valid props.
        '''

        logDebug('CeCommonAllocator: valid_props: props = {} '.format(props))

        if not props:
            return dict()
        if isinstance(props, dict):
            return props
        elif isinstance(props, str) or isinstance(props, unicode):
            props = props.strip()
            try:
                props = ast.literal_eval(props)
            except Exception as e:
                logError('Cannot parse properties: `{}`, `{}` !'.format(props, e))
                return None
        else:
            logError('Invalid properties for set method`{}` !'.format(props))
            return False
        return props


    def generate_index(self):
        '''
        Generate index when creating a new sut or test bed.
        '''

        logDebug('CeCommonAllocator: generate_index')

        res_id = False
        while not res_id:
            res_id = hexlify(os.urandom(5))
            # If by any chance, this ID already exists, generate another one!
            if self.get_id(res_id, self.resources):
                res_id = False
        return res_id


    def format_resource(self, result, query):
        '''
        We have to return data in a certain form - a formated dictionary.
        '''

        logDebug('CeCommonAllocator: format_resource query = {}'.format(query))

        meta = ""
        if ':' in query:
            meta  = query.split(':')[1]
        if not meta:
            result['children'] = sorted([result['children'][node]['id'] for
                node in result.get('children') or []],
                key=lambda node: node.lower())
            result['path'] = '/'.join(result['path'])
            if result['meta'] == '{}':
                result['meta'] = dict()
        else:
            result = result['meta'].get(meta, '')

        return result


    def reserve_resource(self, res_query, props={}):
        """
        reserve a resource for a certain user
        """
        user_info = self.user_info(props)
        logDebug('CeCommonAllocator:reserve_resource {} {} {}'.format(res_query, props, user_info[0]))

        # in case we recive meta info too
        if ':' in res_query:
            res_query = res_query.split(':')[0]

        with self.acc_lock:
            # verify if the resource is locked by other user
            _isResourceLocked = self.is_resource_locked(res_query)
            if _isResourceLocked:
                if _isResourceLocked != user_info[0]:
                    msg = 'User {}: reserve_resource: The resource is locked for {} !'.format(user_info[0],_isResourceLocked)
                    logError(msg)
                    return '*ERROR* ' + msg
                else:
                    msg = 'User {}: reserve_resource: Has already locked this resource: {}'.format(user_info[0], res_query)
                    logDebug(msg)
                    return True
            #verify is the resource is reserved by other user
            _isResourceReserved = self.is_resource_reserved(res_query)
            if _isResourceReserved:
                if _isResourceReserved != user_info[0]:
                    msg = 'User {}: reserve_resource: The resource is reserved for {}!'.format(user_info[0],_isResourceReserved)
                    logError(msg)
                    return '*ERROR* ' + msg
                else:
                    msg = 'User {}: Has already reserved this resource: {}'.format(user_info[0], res_query)
                    logDebug(msg)
                    return True

            node_path = self.get_resource(res_query)

            if not node_path or isinstance(node_path, str):
                msg = "User {}: We do not have this resource {}".format(user_info[0], res_query)
                logError(msg)
                return "*ERROR* " + msg

            #we need to set as reserved the root of this resource
            if len(node_path['path']) > 1:
                node_path = self.get_path(node_path['path'][0], self.resources)

            if not node_path:
                msg = 'User {}: Reserve Resource: Cannot find resource path or ID !'.format(user_info[0])
                logError(msg)
                return '*ERROR* ' + msg

            #adding the resource to reservedResources dictionary
            if user_info[0] in self.reservedResources:
                self.reservedResources[user_info[0]].update([(node_path['id'], copy.deepcopy(node_path)), ])
            else:
                self.reservedResources.update([(user_info[0], {node_path['id']: copy.deepcopy(node_path)}), ])

        return True #RESOURCE_RESERVED


    def is_resource_reserved(self, res_query, props={}):
        """
        Verify if a resource is already reserved.
        Returns the user or false.
        """

        logFull('CeCommonAllocator:is_resource_reserved: res_query = {}'.format(res_query))

        resources = self.resources
        user_info = self.user_info(props)

        if '/' not in res_query:
            reservedForUser = [u for u in self.reservedResources if res_query in self.reservedResources[u]]

        if '/' in res_query or not reservedForUser:
            #if res_query contains components unsaved yet, search only for the TB
            if "/" in res_query:
                parts = [q for q in res_query.split('/') if q]
                node_path = self.get_resource("/" + parts[0])
            else:
                node_path = self.get_resource(res_query)

            if not node_path or isinstance(node_path,str):
                msg = "We do not have this resource {}".format(res_query)
                logError(msg)
                return False

            if len(node_path['path']) > 1:
                node_path = self.get_path(node_path['path'][0], resources)
                if not node_path and isinstance(node_path,str):
                    msg = "We do not have this resource {}".format(res_query)
                    logError(msg)
                    return False

            reservedForUser = [u for u in self.reservedResources if node_path['id'] in self.reservedResources[u]]

        if not reservedForUser:
            return False
        return reservedForUser[0]


    def get_reserved_resource(self, res_query, props={}):
        '''
        Returns the reserved resource.
        '''

        logDebug('CeCommonAllocator:get_reserved_resource: res_query = {}'.format(res_query))

        resources = self.resources
        # If no resources...
        if not resources:
            msg = 'CeCommonAllocator:get_reserved_resource: There are no resources defined !'
            logError(msg)
            return False

        user_info = self.user_info(props)

        if not self.reservedResources.get(user_info[0]):
            msg = 'CeCommonAllocator:get_reserved_resource: Resource `{}` is not reserved !'.format(res_query)
            logError(msg)
            return False

        # get only the root parent in case the path query contains components unsaved yet
        parts = [q for q in res_query.split('/') if q]
        if "/" in res_query:
            parent_path = parts
            node_path = self.get_resource("/" + parts[0])
        # if query is an saved component
        else:
            node_path = self.get_resource(res_query)
            if node_path:
                parent_path = node_path['path']

        #maybe query is an id of a component unsaved yet
        if not node_path:
            for p in self.reservedResources[user_info[0]]:
                node_path = self.get_resource(res_query, self.reservedResources[user_info[0]][p])
                if node_path:
                    self.reservedResources[user_info[0]][p]['path'] = node_path['path']
                    return self.reservedResources[user_info[0]][p]
            msg = 'CeCommonAllocator:get_reserved_resource: Cannot find resource ID in self.reservedResources`{}` !'.format(res_query)
            logError(msg)
            return False

        if len(node_path['path']) > 1:
            node_path = self.get_path(node_path['path'][0], resources)

        self.reservedResources[user_info[0]][node_path['id']]['path'] = parent_path
        logDebug('afisare in get reserved {}'.format(self.reservedResources[user_info[0]][node_path['id']]))
        return self.reservedResources[user_info[0]][node_path['id']]


    def lock_resource(self, res_query, props={}):
        """
        lock the resource res_query by adding it to the self.lockedResources dict
        """
        logDebug('CeCommonAllocator:lock_resource {} {}'.format(res_query, props))
        resources = self.resources
        user_info = self.user_info(props)

        # If no resources...
        if not resources.get('children'):
            msg = 'User {}: Lock resource: There are no resources defined !'.format(user_info[0])
            logError(msg)
            return '*ERROR* ' + msg

        if ':' in res_query:
            res_query = res_query.split(':')[0]

        with self.acc_lock:

            # verify if the resource is locked by other user
            _isResourceLocked = self.is_resource_locked(res_query)
            if _isResourceLocked:
                if _isResourceLocked != user_info[0]:
                    msg = 'User {}: Reserve resource: The resource is locked for {} !'.format(user_info[0],_isResourceLocked)
                    logError(msg)
                    return '*ERROR* ' + msg
                else:
                    msg = 'User {}: Has already locked this resource: {}'.format(user_info[0], res_query)
                    logDebug(msg)
                    return True
            #verify is the resource is reserved by other user
            _isResourceReserved = self.is_resource_reserved(res_query)
            if _isResourceReserved:
                if _isResourceReserved != user_info[0]:
                    msg = ' Reserve resource: The resource is reserved for!'
                    logError(msg)
                    return '*ERROR* ' + msg
                else:
                    msg = 'User {}: Has already reserved this resource: {}'.format(user_info[0], res_query)
                    logDebug(msg)
                    return True

            node = self.get_resource(res_query)
            if not node or isinstance(node,str):
                msg = "We do not have this resource {}".format(res_query)
                logError(msg)
                return "*ERROR* " + msg
            if len(node['path']) > 1:
                node = self.get_path(node['path'][0], resources)

            if isinstance(node,str):
                msg = 'User {}: Lock Resource: Cannot find resource path or ID `{}` !'.format(user_info[0],res_query)
                logError(msg)
                return '*ERROR* ' + msg

            user_res = self.lockedResources.get(user_info[0], {})
            user_res.update({node['id']: copy.deepcopy(node)})
            self.lockedResources[user_info[0]] = user_res

        return True


    def unlock_resource(self, res_query, props={}):
        """
        unlock the resource, delete it from self.lockedResources dict
        """
        logDebug('CeCommonAllocator:unlock_resource {} {} '.format(res_query,props))
        user_info = self.user_info(props)
        #we need the id of the resource so we get it from self.resources

        node = self.get_resource(res_query)
        if isinstance(node,str):
            msg = "We do not have this resource {}".format(res_query)
            logError(msg)
            return "*ERROR* " + msg
        #we need to lock the root of the resource
        if len(node['path']) > 1:
            node = self.get_path(node['path'][0], self.resources)

        if isinstance(node,str):
            msg = 'User {}: Unlock resource: Cannot find resource path or ID `{}` !'.format(user_info[0],res_query)
            logError(msg)
            return '*ERROR* ' + msg

        with self.acc_lock:
            try:
                self.lockedResources[user_info[0]].pop(node['id'])
                if not self.lockedResources[user_info[0]]:
                    self.lockedResources.pop(user_info[0])
            except Exception as e:
                msg = 'User {}: Unlock resource: `{}` !'.format(user_info[0], e)
                logError(msg)
                return "*ERROR* " + msg

        return True #RESOURCE_FREE


    def is_resource_locked(self, res_query):
        """
        check if a resource is in self.lockedResources dict
        returns the user or false
        """

        logFull('CeCommonAllocator:is_resource_locked: res_query = {}'.format(res_query))

        # Having the id we can get it directly
        if '/' not in res_query:
            lockedForUser = [u for u in self.lockedResources if res_query in self.lockedResources[u]]
        # Otherwise get the id from the path or get find the root parent of the resource
        if '/' in res_query or not lockedForUser:
            node = self.get_resource(res_query)
            if not node or isinstance(node, str):
                msg = "CeCommonAllocator: is_resource_locked: We do not have this resource {}".format(res_query)
                logError(msg)
                return False

            if len(node['path']) > 1:
                node = self.get_path(node['path'][0], self.resources)

            if isinstance(node,str):
                logError('CeCommonAllocator: is_resource_locked: Cannot find resource path or ID `{}` !'.format(res_query))
                logError(msg)
                return  False

            lockedForUser = [u for u in self.lockedResources if node['id'] in self.lockedResources[u]]

        if not lockedForUser:
            return False

        return lockedForUser[0]


    def discard_release_reserved_resource(self, res_query, props={}):
        """
        Discard changes for both SUTs and TestBeds
        """

        user_info = self.user_info(props)
        user = user_info[0]
        logDebug('User {}: CeCommonAllocator:discard_release_reserved_resource: {}'.format(user, res_query))

        if ':' in res_query:
            res_query = res_query.split(':')[0]

        # Having the id, we can discard and release directly
        if user in self.reservedResources.keys():

            if res_query in self.reservedResources[user]:
                node_id = res_query
            # Having the name of the resource we have to get the id
            else:
                node = self.get_resource(res_query)
                if not isinstance(node, dict):
                    msg = "Could not found a result for {}".format(res_query)
                    logDebug(msg)
                    return False

                # get the root parent of this resource
                if len(node['path']) > 1:
                    node = self.get_path(node['path'][0], self.resources)

                if not node:
                    logError('User {}: CeCommonAllocator:discard_release_reserved_resource: Cannot find resource path or ID `{}` !'.format(user,res_query))
                    return False
                node_id = node['id']
            # Delete the entry from reserved dict
            try:
                self.reservedResources[user].pop(node_id)
                if not self.reservedResources[user]:
                    self.reservedResources.pop(user)
            except Exception as e:
                logError('CeCommonAllocator:discard_release_reserved_resource: `{}` for user {}!'.format(e, user))
                return False

        return True #RESOURCE_FREE
