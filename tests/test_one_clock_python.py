import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research'/'options_replay'))
from one_clock import select_as_of,observation,reading_as_of,TrainerSource
from tests.test_options_replay import make_pack,RS,OPEN,DAY,DATE_PATTERNS
import re
from copy import deepcopy


class PortableClock(unittest.TestCase):
    def test_shared_js_python_contract(self):
        fixture=json.loads((ROOT/'tests'/'fixtures'/'one_clock_contract.json').read_text())
        rows=[dict(fixture['base'],**row) for row in fixture['rows']]
        for example in fixture['cases']:
            scope=dict(session=fixture['base']['session'],instrument='QQQ',origin=example['origin'],cutoff=fixture['base']['eventAt']+example['offset'])
            for order in (rows,list(reversed(rows))):
                result=select_as_of(order,scope)
                self.assertEqual([[r['id'],r['revision'],r['value']] for r in result],example['expected'])

    def test_retraction_freshness_validation_and_boolean_conflicts(self):
        base=json.loads((ROOT/'tests'/'fixtures'/'one_clock_contract.json').read_text())['base']
        scope=dict(session=base['session'],instrument='QQQ',cutoff=base['eventAt']+1)
        self.assertEqual(reading_as_of([base],scope,0)['state'],'stale')
        self.assertEqual(reading_as_of([dict(base,value=None)],scope,0)['state'],'unavailable')
        with self.assertRaisesRegex(ValueError,'conflicting'):
            select_as_of([base,dict(base,value=True)],scope)
        for changes in [dict(availableAt=base['eventAt']-1),dict(revision=True),dict(eventAt='2026-11-09T10:00:00')]:
            with self.assertRaises(ValueError):observation(dict(base,**changes))

    def test_publication_delay_gates_board_charts_volume_and_bar_steps(self):
        pack=make_pack({OPEN:700,OPEN+60:701})
        pack['snapshots'][1].update(available_s=OPEN+120,volume=999)
        session=RS(pack,start_s=OPEN+60)
        view=session.view()
        self.assertEqual(view['spot']['now'],700)
        self.assertEqual(view['spot']['series'],[[OPEN,700]])
        self.assertEqual(view['volume'],[])
        self.assertEqual(view['session']['clock_origin'],'reconstructed')
        for pattern in DATE_PATTERNS:self.assertIsNone(re.search(pattern,json.dumps(view)))
        view=session.step_bar()
        self.assertEqual(session.clock,OPEN+120)
        self.assertEqual(view['spot']['now'],701)
        self.assertEqual(view['volume'],[[OPEN+60,999]])
        self.assertEqual(view['quotes']['age_s'],60)

    def test_late_transport_cannot_refresh_old_quotes_or_promote_unknown_publication(self):
        pack=make_pack({OPEN:700})
        pack['snapshots'][0]['available_s']=OPEN+240
        session=RS(pack,start_s=OPEN+240)
        self.assertEqual(session.view()['quotes']['code'],'gap')
        pack['snapshots'][0]['available_s']=None
        self.assertEqual(RS(pack,start_s=OPEN+240).view()['board'],[])
        del pack['snapshots'][0]['available_s']
        pack['clock_origin']='recorded'
        self.assertEqual(RS(pack,start_s=OPEN).view()['board'],[])

    def test_correction_and_retraction_match_normal_selector_at_each_boundary(self):
        pack=make_pack({OPEN:700,OPEN+60:701,OPEN+120:702})
        for i,snap in enumerate(pack['snapshots']):
            snap.update(t=OPEN,observation_id='one',revision=i+1,available_s=OPEN+60*i)
        pack['snapshots'][2]['retracted']=True
        source=TrainerSource(pack,'QQQ')
        for seconds in [OPEN-1,OPEN,OPEN+59,OPEN+60,OPEN+119,OPEN+120]:
            scope=dict(session=DAY,instrument='QQQ',origin='reconstructed',cutoff=source.base+seconds*1000)
            expected=select_as_of(source.rows,scope)
            self.assertEqual(source.at(seconds),expected)
            self.assertEqual(source.latest(seconds),expected[-1] if expected else None)
        self.assertIsNone(source.latest(OPEN+120)['value'])

    def test_large_tape_latest_lookup_does_not_materialize_every_prefix(self):
        source=TrainerSource(dict(day=DAY,snapshots=[dict(t=OPEN+i,key=f'fixture/{i}') for i in range(10000)]),'QQQ')
        self.assertEqual(len(source.latest_rows),10000)
        self.assertEqual(source.latest(OPEN+9999)['value'],9999)
        self.assertEqual(source.latest(OPEN-1),None)

    def test_hidden_payload_cannot_change_frozen_age_or_order_eligibility(self):
        views=[]
        for hidden_spot in (700,701):
            pack=make_pack({OPEN:700,OPEN+60:hidden_spot,OPEN+120:700})
            pack['snapshots'][1]['available_s']=OPEN+300
            session=RS(pack,start_s=OPEN+180)
            view=session.view()
            views.append((view['quotes'],view['board'],view['spot']['series']))
            self.assertEqual(view['quotes']['code'],'frozen')
            with self.assertRaisesRegex(ValueError,'frozen'):
                session.order('buy','C700',1)
        self.assertEqual(views[0],views[1])

    def test_content_age_matches_eligible_revision_history(self):
        pack=make_pack({OPEN:700,OPEN+60:701,OPEN+120:700})
        correction=deepcopy(pack['snapshots'][1])
        correction.update(revision=2,available_s=OPEN+150)
        correction['spot']=700
        correction['q']=deepcopy(pack['snapshots'][0]['q'])
        retraction=deepcopy(correction)
        retraction.update(revision=3,available_s=OPEN+210,retracted=True)
        pack['snapshots'].extend([correction,retraction])
        source=TrainerSource(pack,'QQQ')
        for seconds in range(OPEN,OPEN+301):
            previous=None
            content=None
            for row in source.at(seconds):
                snap=pack['snapshots'][row['value']] if row['value'] is not None else None
                sig=(snap['spot'],snap['q']) if snap else ('retracted',row['id'])
                if sig!=previous:content=(row['eventAt']-source.base)/1000
                previous=sig
            self.assertEqual(source.content_time(seconds),content)
        self.assertEqual(source.content_time(OPEN+149),OPEN+120)
        self.assertEqual(source.content_time(OPEN+150),OPEN)
        self.assertEqual(source.content_time(OPEN+210),OPEN+120)

    def test_duplicate_payloads_collapse_but_immutable_conflicts_fail(self):
        pack=make_pack({OPEN:700})
        pack['snapshots'].append(deepcopy(pack['snapshots'][0]))
        source=TrainerSource(pack,'QQQ')
        self.assertEqual(len(source.at(OPEN)),1)
        self.assertEqual(source.latest(OPEN)['value'],0)
        self.assertEqual(RS(pack,start_s=OPEN).view()['quotes']['status'],'live')
        for retracted in (False,True):
            bad=deepcopy(pack)
            for snap in bad['snapshots']:snap['retracted']=retracted
            bad['snapshots'][1]['spot']=701
            source=TrainerSource(bad,'QQQ')
            with self.assertRaisesRegex(ValueError,'conflicting'):source.latest(OPEN)
            with self.assertRaisesRegex(ValueError,'conflicting'):source.at(OPEN)


if __name__=='__main__':unittest.main()
