"""
<title>MeterConfigRequest</title>
<description>
    Request configuration for a meter
    
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

class MeterConfigRequest(SimpleProtocol):
    """
    Request configuration for a meter
    """
    def runTest(self):
        self.logger.info("Running MeterConfigRequest test")
        self.logger.info("Sending meter_config_request")

        self.logger.info("Delete all meters")
        rc = testutils.delete_all_meters(self.controller, self.logger)
        self.assertEqual(rc, 0, "Failed to delete all flows")

        msg = message.meter_mod()
        msg.command = ofp.OFPMC_ADD
        msg.meter_id = 1
        msg.flags = ofp.OFPMF_KBPS
        band1 = meter.meter_band_drop()
        band1.rate = 1024
        band1.burst_size = 12
        msg.bands.add(band1)
        logMsg('logDebug',"Request send to switch:")
        logMsg('logDebug',msg.show())
        rv = self.controller.message_send(msg)

        self.logger.info("Send meter config request")
        msg = message.meter_config_request()
        msg.meter_id = 1
        logMsg('logDebug',"Request send to switch:")
        logMsg('logDebug',msg.show())
        rv, _ = self.controller.transact(msg, timeout= 2)
        logMsg('logDebug',"Response from switch:")
        logMsg('logDebug',rv.show())
        self.assertTrue(rv is not None, "Switch did not reply")
        self.assertTrue(rv.type == ofp.OFPMP_METER_CONFIG, "Switch must return OFPMP_METER_CONFIG")

    
tc = MeterConfigRequest()
_RESULT = tc.run()
