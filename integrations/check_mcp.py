"""Bounded real-source verification. Prints evidence booleans, never financial rows."""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qianji.mcp_reader import Reader
from qianji.analysis import load_publication


def metadata():
    return {str(p.relative_to(ROOT/'data')):(p.stat().st_size,p.stat().st_mtime_ns)
            for p in (ROOT/'data').rglob('*') if p.is_file()}


async def main():
    before=metadata()
    report={'protocols':[], 'personal_values_printed':False, 'import_or_publication_run':False}
    for mode in ('auto','legacy'):
        params=StdioServerParameters(command=sys.executable,
             args=['-B','-X','utf8','-u','-m','qianji.mcp_server'],cwd=str(ROOT))
        async with Client(params,read_timeout_seconds=60,cache=None,mode=mode) as client:
            tools=(await client.list_tools()).tools
            assert {t.name for t in tools} == {'qianji_snapshot','qianji_catalog','qianji_read','qianji_original','qianji_totals','qianji_breakdown','qianji_field_inventory','qianji_analysis_snapshot','qianji_labels',
                'qianji_analytics_snapshot','qianji_analytics_catalog','qianji_analytics_facts',
                'qianji_analytics_cube','qianji_analytics_quality'}
            assert all(t.annotations.read_only_hint for t in tools)
            async def call(name,args=None):
                result=await client.call_tool(name,args or {})
                assert not result.is_error, name+' failed'
                return result.structured_content
            status=await call('qianji_snapshot'); ref=status['snapshot_ref']
            fields=[]; offset=0
            while offset is not None:
                page=await call('qianji_catalog',{'offset':offset})
                fields.extend(f['name'] for f in page['fields']);offset=page['next_offset']
            assert 'source_file_sha256' in fields and 'source_row' in fields and 'remark' in fields
            first=await call('qianji_read',{'snapshot_ref':ref,'limit':1})
            assert first['data'], 'Published source has no row for real-record validation'
            row=first['data'][0]
            raw=await call('qianji_original',{'snapshot_ref':ref,'record_id':row['id'],'pointer':'/money'})
            assert Decimal(raw['json_text']) == Decimal(row['amount'])
            assert raw['source_file_sha256'] == row['source_file_sha256'] and raw['source_row'] == row['source_row']
            filters={'id':row['id']}
            total=await call('qianji_totals',{'snapshot_ref':ref,'filters':filters})
            reader=Reader(ROOT/'data')
            assert total==reader.totals(ref,filters)
            assert total['matched_records']==1
            # Independent whole-selection reconciliation; output only evidence booleans.
            expected={}
            source_rows=reader._rows(reader._open(ref),'transactions',{})
            for item in source_rows:
                key=(item['ledger_id'],item['currency'],item['category'])
                group=expected.setdefault(key,{'count':0,**{k:Decimal(0) for k in ('expense','income','refund','transfer')}})
                group['count']+=1;group[item['kind']]+=Decimal(item['amount'])
            actual={};offset=0
            while offset is not None:
                page=await call('qianji_breakdown',{'snapshot_ref':ref,'offset':offset})
                for group in page['data']:
                    key=(group['ledger_id'],group['currency'],group['category'])
                    assert key not in actual
                    actual[key]={'count':group['count'],**{k:Decimal(group[k]) for k in ('expense','income','refund','transfer')}}
                offset=page['pagination']['next_offset']
            assert actual==expected
            assert page['matched_records']==len(source_rows)
            inventory_fields=[];offset=0
            while offset is not None:
                page=await call('qianji_field_inventory',{'snapshot_ref':ref,'offset':offset,'limit':5})
                inventory_fields.extend(page['data']);offset=page['pagination']['next_offset']
            assert page['verified_records']==len(source_rows)
            assert len({f['source_field'] for f in inventory_fields})==len(inventory_fields)
            assert all('value' not in f for f in inventory_fields)
            analysis=await call('qianji_analysis_snapshot',{'snapshot_ref':ref})
            if analysis['available']:
                pub=load_publication(reader,ref,analysis['analysis_ref'])
                states=analysis['coverage']['model_status_counts']
                assert sum(states.values())==len(source_rows)
                assert all(states[s]==sum(e.get('model_status','unreported')==s for e in pub['entries'].values()) for s in states)
                report['model_processing_states_reconcile']=True
                report['model_processing_states_reported']=states['unreported']==0
                latest=max(r['date_local'] for r in source_rows)
                filters={'start_date':latest[:7]+'-01','end_date':latest}
                selected=reader._rows(reader._open(ref),'transactions',filters)
                expected_ids={r['id'] for r in selected if any(t['state']=='proposed' for t in pub['entries'][r['id']]['labels'])}
                actual_ids=set();offset=0
                while offset is not None:
                    page=await call('qianji_labels',{'snapshot_ref':ref,'analysis_ref':analysis['analysis_ref'],
                        'filters':filters,'include_proposed':True,'only_proposed':True,'offset':offset,'limit':2})
                    for item in page['data']:
                        assert item['id'] not in actual_ids
                        actual_ids.add(item['id'])
                        assert item['labels']==pub['entries'][item['id']]['labels']
                    offset=page['pagination']['next_offset']
                assert actual_ids==expected_ids
                expected_totals={}
                for item in selected:
                    if item['id'] not in expected_ids:continue
                    g=expected_totals.setdefault((item['ledger_id'],item['currency']),{'count':0,**{k:Decimal(0) for k in ('expense','income','refund','transfer')}})
                    g['count']+=1;g[item['kind']]+=Decimal(item['amount'])
                actual_totals={(t['ledger_id'],t['currency']):{'count':t['count'],**{k:Decimal(t[k]) for k in ('expense','income','refund','transfer')}} for t in page['totals']}
                assert actual_totals==expected_totals
                default=await call('qianji_labels',{'snapshot_ref':ref,'analysis_ref':analysis['analysis_ref'],'filters':filters})
                short=await call('qianji_labels',{'snapshot_ref':ref,'analysis_ref':analysis['analysis_handle'],'filters':filters})
                assert short==default
                assert (await client.call_tool('qianji_labels',{'snapshot_ref':ref,'analysis_ref':'qa_999999'})).is_error
                assert all(t['state']!='proposed' for item in default['data'] for t in item['labels'])
                report.update(candidate_pages_and_full_totals_reconcile=True,default_excludes_model_proposals=True,short_handle_pins_same_publication=True)
            analytics=await call('qianji_analytics_snapshot')
            catalog=await call('qianji_analytics_catalog')
            assert catalog['dataset']=='catalog' and catalog['taxonomy_version']==catalog['taxonomy']['version']
            report['analytics_available']=analytics['available']
            if analytics['available']:
                analytics_ref=analytics['analytics_ref']
                pin_keys=('analytics_ref','source_snapshot_ref','analysis_ref','analysis_source_snapshot_ref',
                          'label_source_state','taxonomy_version','projection_version','recipe')
                pin={key:analytics[key] for key in pin_keys}
                facts=await call('qianji_analytics_facts',{'analytics_ref':analytics_ref,'limit':1})
                cube=await call('qianji_analytics_cube',{'analytics_ref':analytics_ref,
                    'group_by':['month','category_l2','channel'],'limit':1})
                quality=await call('qianji_analytics_quality',{'analytics_ref':analytics_ref})
                for result in (facts,cube,quality):
                    assert {key:result[key] for key in pin_keys}==pin
                    assert result['format']=='qianji-analytics-query' and result['contract_version']=='1.0'
                base_rows=reader._open(analytics['source_snapshot_ref'])['datasets']['transactions']
                expected_totals={}
                for item in base_rows:
                    g=expected_totals.setdefault((item['ledger_id'],item['currency']),{'count':0,
                        **{k:Decimal(0) for k in ('expense','income','refund','transfer')}})
                    g['count']+=1;g[item['kind']]+=Decimal(item['amount'])
                actual_totals={(t['ledger_id'],t['currency']):{'count':t['count'],
                    **{k:Decimal(t[k]) for k in ('expense','income','refund','transfer')}} for t in cube['totals']}
                assert actual_totals==expected_totals
                assert facts['matched_records']==cube['matched_records']==quality['matched_records']==len(base_rows)
                if facts['data']:
                    first=facts['data'][0]
                    base=next(row for row in base_rows if row['id']==first['id'])
                    assert all(first[key]==value for key,value in base.items())
                    assert set(first['classification'])==set(catalog['taxonomy']['dimensions'])
                if cube['data']:
                    drill=await call('qianji_analytics_facts',{'analytics_ref':analytics_ref,
                        'filters':cube['data'][0]['trace']['filters'],'limit':1})
                    assert drill['matched_records']==cube['data'][0]['count']
                assert (await client.call_tool('qianji_analytics_cube',{'analytics_ref':analytics_ref,
                    'group_by':['month','month']})).is_error
                report.update(analytics_fixed_references_verified=True,
                    analytics_three_dimensions_reconcile=True,analytics_drilldown_verified=True,
                    analytics_quality_verified=True)
            assert (await client.call_tool('qianji_read',{'snapshot_ref':ref,'filters':{'sql':'SELECT 1'}})).is_error
            assert (await client.call_tool('qianji_original',{'snapshot_ref':ref,'record_id':'../data'})).is_error
            report['protocols'].append(client.protocol_version)
            report.update(tool_count=len(tools),standard_fields=len(fields),source_record_amount_equal=True,
                          raw_field_inventory_verified=True,analysis_available=analysis['available'],
                          archive_hash_row_and_normalization_checked=True,retention=status['retention'],
                          all_category_pages_reconcile_with_source=True,
                          nonempty_categories_present=any(x['category'].strip() for x in source_rows),
                          nonempty_remarks_present=any((x['remark'] or '').strip() for x in source_rows))
    report['data_file_metadata_unchanged'] = before==metadata()
    assert report['data_file_metadata_unchanged'], 'Source changed concurrently; cannot attest unchanged'
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
