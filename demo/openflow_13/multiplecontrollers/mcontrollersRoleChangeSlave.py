"""
<title>RoleChangeSlave</title>
<description>Send request to change role to SLAVE, verify 
</description>
"""

try:
    if('TWISTER_ENV' in globals()):
        from ce_libs.openflow.of_13.openflow_base import *
        testbed=currentTB
        from ce_libs import ra_proxy
        ra_service=ra_proxy                        
except:
    raise

class RoleChangeSlave(SimpleProtocol):

    """Send request to change role to SLAVE, verify """

    def runTest(self):

        self.logger.info("Running change role to slave test ")
        self.logger.info("Sending OFPT_ROLE_REQUEST ")
        self.logger.info("Expecting OFPT_ROLE_REPLY with role slave ")

        #Get the current generation_id
        request = message.role_request()
        request.role = ofp.OFPCR_ROLE_NOCHANGE
        logMsg('logDebug',"Request to switch: ")
        logMsg('logDebug',request.show())
        rv = self.controller.message_send(request)
        self.assertTrue(rv != -1, " Not able to send role request.")
        (response, pkt) = self.controller.poll(exp_msg=ofp.OFPT_ROLE_REPLY,
                                               timeout=2)
        self.assertTrue(response is not None, 'Did not receive OFPT_ROLE_REPLY')
        logMsg('logDebug',"Response from switch: ")
        logMsg('logDebug',response.show())
        new_genid = response.generation_id + 1

        #Send role change request
        request = message.role_request()
        request.role = ofp.OFPCR_ROLE_SLAVE
        request.generation_id = new_genid
        logMsg('logDebug',"Request to switch: ")
        logMsg('logDebug',request.show())
        rv = self.controller.message_send(request)
        self.assertTrue(rv != -1, "Not able to send role request.")
        (response, pkt) = self.controller.poll(exp_msg=ofp.OFPT_ROLE_REPLY,
                                               timeout=2)
        self.assertTrue(response is not None, 'Did not receive OFPT_ROLE_REPLY')
        logMsg('logDebug',"Response from switch: ")
        logMsg('logDebug',response.show())
        self.assertTrue(response.role == ofp.OFPCR_ROLE_SLAVE, 'Role is not SLAVE')

    
tc = RoleChangeSlave()
_RESULT = tc.run()
