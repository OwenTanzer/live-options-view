from types import SimpleNamespace as NS
from unittest import TestCase
from market_data.ibkr_mbt.bridge import identity


class MicroIdentityTests(TestCase):
    def test_matches_provider_underlying_and_settlement_date(self):
        opt = NS(secType='FOP',symbol='MBT',exchange='CME',currency='USD',multiplier='0.1',
                 right='C',conId=123,strike=59000,localSymbol='example')
        detail = NS(contract=opt,underConId=45,realExpirationDate='20261127',
                    lastTradeTime='16:00:00',timeZoneId='Europe/London')
        fut = NS(contract=NS(conId=45,localSymbol='MBTX6'),realExpirationDate='20261127')
        self.assertEqual(identity(detail,fut)['contract_key'],'MBTOPT:123')
        detail.underConId=46
        self.assertIsNone(identity(detail,fut))
        detail.underConId=45
        opt.multiplier='5'
        self.assertIsNone(identity(detail,fut))
