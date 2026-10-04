import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lobster_quant_agent'))
"""Offline report regressions: no quotes, model calls, notifications or user state IO."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import pandas as pd
except ImportError:
    pd = None  # Only optional AkShare/DataFrame integration checks need pandas.
import cli as stock
import report_pipeline as reports
from channel_adapters import get_channel_adapter


class ReportReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(stock, 'WATCHLIST_PATH', str(Path(self.tmp.name) / 'watch.json')))
        self.stack.enter_context(patch.object(stock, 'MARKET_OVERVIEW_CACHE_PATH', str(Path(self.tmp.name) / 'cache.json')))
        self.stack.enter_context(patch.object(stock.requests, 'get', side_effect=AssertionError('network must be mocked')))
        self.stack.enter_context(patch.object(stock, 'send_monitor_notification', side_effect=AssertionError('no notification')))
        self.day = stock._normalize_report_date()
        self.iso_day = datetime.strptime(self.day, '%Y%m%d').strftime('%Y-%m-%d')
        self.row = {'代码': '600001', '名称': '合成样本', '最新价': 10, '涨跌幅%': 1,
                    '数据时间': self.iso_day + ' 15:00:00', '数据源': 'fixture'}
        self.width = {'市场宽度': {'上涨家数': 1, '下跌家数': 1, '平盘家数': 0,
                                 '总成交额_元': 100000000}, '数据源': 'fixture'}
        self.cfg = {'watch_pool': [{'code': '600001', 'name': '样本'}],
                    'holding_pool': [{'code': '600001', 'name': '样本'}], 'monitor': {'enabled': False}}

    def collect(self, **kwargs):
        with patch.object(stock, 'index_sina', return_value=[self.row]), \
             patch.object(stock, 'market_overview_sina', return_value=self.width), \
             patch.object(stock, '_safe_quote_for_report', return_value=dict(self.row)) as quote, \
             patch.object(stock, 'load_watchlist_config', return_value=self.cfg), \
             patch.object(stock, 'lhb', return_value={'date': self.day, 'records': [], 'rows': 0}) as lhb:
            result = stock.after_close_report(emit=False, **kwargs)
        return result, quote, lhb

    def test_historical_report_never_uses_live_quote_or_width(self):
        with patch.object(stock, 'index_sina', side_effect=AssertionError('live index')), \
             patch.object(stock, 'market_overview_sina', side_effect=AssertionError('live breadth')), \
             patch.object(stock, '_safe_quote_for_report', side_effect=AssertionError('live quote')), \
             patch.object(stock, '_historical_quote_for_report', return_value={'代码': '600001', 'error': 'no history'}) as history, \
             patch.object(stock, 'load_watchlist_config', return_value=self.cfg), \
             patch.object(stock, 'lhb', return_value={'records': [], 'rows': 0}) as lhb:
            result = stock.after_close_report(date='2024-01-02', symbol='600001', include_lhb=True, emit=False)
        self.assertEqual(result['date'], '20240102')
        self.assertTrue(history.called)
        self.assertTrue(all(call.args[1] == '20240102' for call in history.call_args_list))
        lhb.assert_called_once_with(date='20240102', symbol='600001', emit=False)
        self.assertIn('历史全A', result['sections']['全A市场概览']['error'])
        self.assertIn('不代表', result['warnings'][0])

    def test_history_exact_date_bar_and_timeout(self):
        response = Mock()
        response.json.return_value = {'data': {'name': '合成', 'klines': ['2024-01-02,9,10,11,8,123,234,1,2,1,4']}}
        with patch.object(stock.requests, 'get', return_value=response) as request:
            result = stock._historical_quote_for_report('600001', '20240102')
        self.assertEqual(result['收盘价'], 10)
        self.assertEqual(result['涨跌幅%'], 2)
        self.assertEqual(request.call_args.kwargs['timeout'], 10)
        self.assertEqual(request.call_args.kwargs['params']['beg'], '20240102')
        self.assertEqual(request.call_args.kwargs['params']['end'], '20240102')

    def test_history_wrong_date_does_not_substitute(self):
        response = Mock()
        response.json.return_value = {'data': {'klines': ['2024-01-03,9,10,11,8,123,234,1,2,1,4']}}
        with patch.object(stock.requests, 'get', return_value=response):
            self.assertIn('error', stock._historical_quote_for_report('600001', '20240102'))

    def test_history_index_market_prefix(self):
        response = Mock(); response.json.return_value = {'data': None}
        with patch.object(stock.requests, 'get', return_value=response) as request:
            stock._historical_quote_for_report('000001', '20240102', index_code='sh000001')
        self.assertEqual(request.call_args.kwargs['params']['secid'], '1.000001')

    def test_invalid_future_date_rejected_without_collection(self):
        for date in ['bad', '20240230', '29990101']:
            with self.subTest(date=date), patch.object(stock, 'index_sina') as index:
                result = stock.report_output('after_close_report', date=date)
                self.assertFalse(result['ok']); index.assert_not_called()

    def test_today_lhb_scoped_to_same_day(self):
        result, _, lhb = self.collect(include_lhb=True, symbol='600001')
        lhb.assert_called_once_with(date=self.day, symbol='600001', emit=False)
        self.assertEqual(result['date'], self.day)

    def test_duplicate_symbol_collected_once(self):
        _, quote, _ = self.collect(symbol='600001')
        codes = [call.args[0] for call in quote.call_args_list]
        self.assertEqual(codes.count('600001'), 1)

    def test_old_quote_not_used_in_judgement(self):
        self.row['数据时间'] = '2000-01-01 15:00:00'
        self.row['涨跌幅%'] = 80
        data, _, _ = self.collect()
        rendered = reports.render_report(data, 'after_close_report')
        self.assertEqual(rendered['analysis']['status'], 'WARN')
        self.assertNotIn('80.00%', rendered['analysis']['summary'])
        self.assertIn('不一致', rendered['text'])

    def test_intraday_quote_not_called_a_close(self):
        self.row['数据时间'] = self.iso_day + ' 10:15:00'
        data, _, _ = self.collect()
        self.assertIn('尚未收盘', data['sections']['指数概览'][0]['error'])

    def test_cache_stale_missing_future_rejected(self):
        for cached_at in ['2001-01-01 00:00:00', None, (datetime.now()+timedelta(hours=1)).isoformat()]:
            with self.subTest(cached_at=cached_at):
                Path(stock.MARKET_OVERVIEW_CACHE_PATH).write_text(json.dumps({'cached_at': cached_at, **self.width}))
                self.assertIsNone(stock.load_market_overview_cache())

    def test_cache_fresh_labeled_and_warned(self):
        stock.save_market_overview_cache(self.width)
        data = stock.load_market_overview_cache()
        self.assertTrue(data['使用缓存'])
        analysis = reports.build_after_close_analysis({'sections': {'全A市场概览': data, '指数概览': [self.row]}})
        self.assertEqual(analysis['status'], 'WARN')
        self.assertIn('缓存', ' '.join(analysis['warnings']))

    @unittest.skipIf(pd is None, "requires optional akshare/pandas DataFrame support")
    def test_missing_spot_values_not_zero_or_flat(self):
        frame = pd.DataFrame([{'代码': '600001', '涨跌幅': '-', '成交额': None},
                              {'代码': '600002', '涨跌幅': '0', '成交额': 'nan'},
                              {'代码': '600003', '涨跌幅': float('inf'), '成交额': float('inf')}])
        normalized = stock._normalize_spot_df(frame)
        self.assertEqual(int((normalized['涨跌幅'] == 0).sum()), 1)
        self.assertEqual(int(normalized['涨跌幅'].isna().sum()), 2)
        self.assertTrue(pd.isna(normalized['成交额'].sum(min_count=1)))

    @unittest.skipIf(pd is None, "requires optional akshare/pandas DataFrame support")
    def test_market_width_marks_missing_sample_and_totals(self):
        fake = Mock()
        fake.stock_zh_a_spot.return_value = pd.DataFrame([{'代码': '600001', '涨跌幅': None, '成交额': None}, {'代码': '600002', '涨跌幅': 0, '成交额': None}])
        with patch.dict('sys.modules', {'akshare': fake}):
            data = stock.market_overview_sina()
        self.assertEqual(data['市场宽度']['平盘家数'], 1)
        self.assertEqual(data['市场宽度']['涨跌幅缺失家数'], 1)
        self.assertIsNone(data['市场宽度']['总成交额_元'])

    def test_nonfinite_not_formatted_or_in_average(self):
        for value in [float('nan'), float('inf'), '-inf']:
            self.assertIsNone(stock._to_float(value))
            self.assertEqual(reports._number(value), '暂无')
        self.assertNotIn('nan', stock._after_market_judgement([{'涨跌幅%': float('nan')}]))

    def lhb_row(self, **extra):
        return {'代码': '600001', '名称': '样本', '上榜日': '2024-01-02', '龙虎榜买入额': 100,
                '龙虎榜卖出额': 20, '龙虎榜净买额': 80, '涨跌幅': 2, **extra}

    def test_lhb_duplicate_reasons_do_not_double_money(self):
        rows = [self.lhb_row(上榜原因='原因一'), self.lhb_row(上榜原因='原因二')]
        result = stock.analyze_lhb_enhanced({'records': rows})
        self.assertEqual(result['龙虎榜完整统计']['合计净买额'], 80)
        self.assertEqual(result['龙虎榜完整统计']['去重后个股数'], 1)

    def test_lhb_conflicting_windows_not_summed(self):
        result = stock.analyze_lhb_enhanced({'records': [self.lhb_row(), self.lhb_row(龙虎榜净买额=200)]})
        self.assertIsNone(result['龙虎榜完整统计']['合计净买额'])
        self.assertEqual(result['龙虎榜完整统计']['金额缺失或冲突数'], 1)
        self.assertTrue(result['数据提示'])

    def test_lhb_missing_money_not_flat(self):
        result = stock.analyze_lhb_enhanced({'records': [{'代码': '600001'}]})
        stats = result['龙虎榜完整统计']
        self.assertEqual(stats['净额接近零个股数'], 0)
        self.assertIsNone(stats['合计净买额'])

    def test_lhb_different_dates_separate(self):
        result = stock.analyze_lhb_enhanced({'records': [self.lhb_row(), self.lhb_row(上榜日='2024-01-03')]})
        self.assertEqual(result['龙虎榜完整统计']['去重后日期个股数'], 2)
        self.assertEqual(result['龙虎榜完整统计']['合计净买额'], 160)

    def test_lhb_truncation_does_not_claim_whole_market(self):
        result = stock.analyze_lhb_enhanced({'records': [self.lhb_row()], 'rows': 200})
        self.assertIn('部分', result['龙虎榜资金方向判断']['结论'])

    def test_lhb_errors_stay_errors(self):
        self.assertEqual(stock.analyze_lhb_enhanced({'error': '接口断开'})['error'], '接口断开')

    @unittest.skipIf(pd is None, "requires optional akshare/pandas DataFrame support")
    def test_lhb_collects_all_rows_not_head30(self):
        fake = Mock()
        fake.stock_lhb_detail_em.return_value = pd.DataFrame([self.lhb_row(代码=f'{600000+i}') for i in range(41)])
        with patch.dict('sys.modules', {'akshare': fake}):
            data = stock.lhb(date='20240102', emit=False)
        self.assertEqual(len(data['records']), 41)
        fake.stock_lhb_detail_em.assert_called_once_with(start_date='20240102', end_date='20240102')

    @unittest.skipIf(pd is None, "requires optional akshare/pandas DataFrame support")
    def test_lhb_stock_exact_date_not_sixty_days(self):
        fake = Mock(); fake.stock_lhb_detail_em.return_value = pd.DataFrame([self.lhb_row()])
        with patch.dict('sys.modules', {'akshare': fake}):
            stock.lhb(date='20240102', symbol='600001', emit=False)
        fake.stock_lhb_detail_em.assert_called_once_with(start_date='20240102', end_date='20240102')

    def test_holding_after_close_routes_without_clarification(self):
        self.assertEqual(stock.classify_after_close_intent('持仓盘后复盘')['intent'], 'holding_review')
        with patch.object(stock, 'load_watchlist_config', return_value=self.cfg), patch.object(stock, 'report_output', return_value={}) as output:
            stock.handle_natural_language_command('持仓盘后复盘')
        self.assertEqual(output.call_args.kwargs['scope'], 'holding_pool')

    def test_named_stock_not_routed_to_market(self):
        with patch.object(stock, 'report_output', return_value={}) as output:
            stock.handle_natural_language_command('贵州茅台盘后复盘')
        self.assertEqual(output.call_args.kwargs['symbol'], '600519')

    def test_review_date_forwarded_from_language(self):
        with patch.object(stock, 'report_output', return_value={}) as output:
            stock.handle_natural_language_command('2024-01-02 大盘复盘')
        self.assertEqual(output.call_args.kwargs['date'], '2024-01-02')

    def test_explicit_combined_scope_does_not_reask(self):
        with patch.object(stock, 'report_output', return_value={'ok': True}) as output:
            result = stock.handle_natural_language_command('大盘和持仓一起盘后复盘')
        self.assertTrue(output.called); self.assertNotIn('needs_clarification', result)

    def test_unspecified_stock_asks_code(self):
        self.assertEqual(stock.classify_after_close_intent('个股复盘')['intent'], 'stock_review')

    def test_stock_report_includes_requested_symbol(self):
        data, _, _ = self.collect(symbol='600001')
        for channel in ['telegram', 'weixin']:
            for variant in ['full', 'simple']:
                rendered = reports.render_report(data, 'after_close_report', variant, channel)
                self.assertEqual(rendered['analysis']['sections'][0]['id'], 'stock')
                self.assertIn('600001', rendered['text'])
                self.assertIn(self.day, rendered['text'])

    def test_holding_report_leads_with_holdings(self):
        data, _, _ = self.collect(); data['review_scope'] = 'holding_pool'
        analysis = reports.build_after_close_analysis(data)
        self.assertEqual(analysis['sections'][0]['id'], 'holding')
        self.assertIn('组合收益', analysis['summary'])

    def test_full_and_simple_both_preserve_cache_warning(self):
        for variant in ['full', 'simple']:
            result = reports.render_report({'sections': {'全A市场概览': {**self.width, '使用缓存': True, 'cached_at': 'x'}, '指数概览': [self.row]}}, 'after_close_report', variant)
            self.assertIn('缓存', result['text'])

    def test_legacy_simple_has_indices_and_date(self):
        data, _, _ = self.collect()
        with patch.object(stock, 'after_close_report', return_value=data):
            simple = stock.after_close_report_simple()
        self.assertIn('主要指数', simple['sections']); self.assertEqual(simple['date'], self.day)

    def test_empty_news_is_warn_and_does_not_claim_direction(self):
        analysis = reports.build_morning_analysis({'sections': {'隔夜/盘前重要消息': []}})
        self.assertEqual(analysis['status'], 'WARN'); self.assertIn('证据不完整', analysis['summary'])

    def test_undated_news_not_claimed_overnight(self):
        analysis = reports.build_morning_analysis({'sections': {'隔夜/盘前重要消息': [{'新闻标题': '合成标题'}]}})
        self.assertIn('不能确认', ' '.join(analysis['warnings']))

    def test_malformed_sections_width_no_crash(self):
        for data in [{'sections': []}, {'sections': {'全A市场概览': {'市场宽度': None}}}]:
            self.assertEqual(reports.build_after_close_analysis(data)['status'], 'WARN')

    def test_bad_nested_model_json_rejected(self):
        good = reports.fallback_analysis('after_close_report')
        for bad in [{'sections': [None]}, {'sections': [{'id': 'x', 'title': 'x', 'items': 'abc'}]},
                    {'warnings': 'bad'}, {'summary': {'wrong': 'type'}}, {'variant': 'simple'}, {'status': 'MAGIC'}]:
            candidate = {**good, **bad}
            _, meta = reports.parse_analysis_candidates([candidate], 'after_close_report')
            self.assertTrue(meta['fallback_used'])
        _, meta = reports.parse_analysis_candidates([good], 'after_close_report')
        self.assertTrue(meta['ok'])

    def test_fallback_reason_visible_and_sensitive_exception_hidden(self):
        with patch.object(stock, 'after_close_report', side_effect=RuntimeError('secret=PRIVATE')):
            result = stock.report_output('after_close_report')
        self.assertFalse(result['ok']); self.assertIn('RuntimeError', result['text']); self.assertNotIn('PRIVATE', json.dumps(result))

    def test_bad_channel_fails_before_data_collection(self):
        with patch.object(stock, 'after_close_report') as collect:
            result = stock.report_output('after_close_report', channel='unsupported')
        self.assertFalse(result['ok']); collect.assert_not_called()

    def test_partial_collection_not_reported_fully_complete(self):
        with patch.object(stock, 'after_close_report', return_value={'sections': {}}):
            result = stock.report_output('after_close_report')
        self.assertTrue(result['partial']); self.assertFalse(result['pipeline']['collection_result']['ok'])
        self.assertEqual(result['analysis']['status'], 'WARN')

    @unittest.skipIf(pd is None, "requires optional akshare/pandas DataFrame support")
    def test_all_missing_primary_breadth_falls_back(self):
        fake = Mock()
        fake.stock_zh_a_spot.return_value = pd.DataFrame([{'代码': '600001', '涨跌幅': None}])
        fake.stock_zh_a_spot_em.return_value = pd.DataFrame([{'代码': '600001', '涨跌幅': 1}])
        with patch.dict('sys.modules', {'akshare': fake}):
            data = stock.market_overview_sina()
        fake.stock_zh_a_spot_em.assert_called_once()
        self.assertEqual(data['市场宽度']['上涨家数'], 1)
        self.assertIn('东方财富', data['数据源'])

    @unittest.skipIf(pd is None, "requires optional akshare/pandas DataFrame support")
    def test_all_missing_breadth_never_claims_zero_up_zero_down(self):
        fake = Mock()
        fake.stock_zh_a_spot.return_value = pd.DataFrame([{'代码': '600001', '涨跌幅': None}])
        fake.stock_zh_a_spot_em.return_value = pd.DataFrame([{'代码': '600001', '涨跌幅': None}])
        with patch.dict('sys.modules', {'akshare': fake}):
            data = stock.market_overview_sina()
        self.assertIn('error', data)
        self.assertNotIn('市场宽度', data)

    def test_all_cli_report_variants_forward_symbol_and_date(self):
        for command in ['after', 'after_full', 'after_simple']:
            with self.subTest(command=command), patch.object(stock.sys, 'argv', ['a_stock_query.py', command, '600001', '--date', '2024-01-02']), patch.object(stock, 'report_output', return_value={'text': 'fixture'}) as output, patch('sys.stdout', new_callable=io.StringIO):
                stock.main()
            self.assertEqual(output.call_args.kwargs['date'], '20240102')
            self.assertEqual(output.call_args.kwargs['symbol'], '600001')

    def test_missing_cli_argument_rejected(self):
        for args in [['--date'], ['--channel'], ['--date', '--full'], ['600001', '600002']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                stock._report_cli_options(args)

    def test_historical_morning_rejected_without_current_news(self):
        with patch.object(stock, 'morning_report') as news:
            result = stock.report_output('morning_report', date='20240102')
        self.assertFalse(result['ok']); news.assert_not_called()
        self.assertIn('历史', result['text'])

    def test_zero_or_missing_close_excluded(self):
        for price in [0, None, float('nan')]:
            self.assertIn('error', stock._dated_report_quote({**self.row, '最新价': price}, self.day))

    def test_pool_mutations_are_not_quote_or_review_intents(self):
        for text in ['持仓池加入 600001 样本B', '持仓池删除 600001', '观察池新增 600001 样本B', '观察池移除 600001']:
            with self.subTest(text=text):
                self.assertEqual(stock.classify_after_close_intent(text)['intent'], 'other')

    def test_natural_pool_add_remove_reaches_mutation_handler(self):
        for pool_name, pool_key in [('持仓池', 'holding_pool'), ('观察池', 'watch_pool')]:
            with self.subTest(pool=pool_name), patch.object(stock, '_safe_quote_for_report', side_effect=AssertionError('not a quote')), patch.object(stock, 'report_output', side_effect=AssertionError('not a report')):
                added = stock.handle_natural_language_command(pool_name + '加入 600001 样本B')
                self.assertTrue(added['ok']); self.assertEqual(added['pool'], pool_key)
                self.assertEqual(stock.load_watchlist_config()[pool_key][0]['code'], '600001')
                removed = stock.handle_natural_language_command(pool_name + '删除 600001')
                self.assertEqual(removed['removed'], 1)
                self.assertEqual(stock.load_watchlist_config()[pool_key], [])

    def test_pool_note_with_review_word_stays_a_pool_command(self):
        with patch.object(stock, 'report_output', side_effect=AssertionError('not a report')):
            added = stock.handle_natural_language_command('持仓池加入 600519 贵州茅台 盘后复盘关注')
        self.assertEqual(added['action'], 'add')
        self.assertEqual(added['item']['note'], '盘后复盘关注')


if __name__ == '__main__':
    unittest.main()
