"""Offline calendar, session boundary, and monitor gate regression tests."""
import contextlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import cli as stock


class MarketCalendarTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for method in ("get", "post"):
            self.stack.enter_context(mock.patch.object(stock.requests, method, side_effect=AssertionError("network forbidden"), create=True))
        self.stack.enter_context(mock.patch.object(stock, "send_monitor_notification", side_effect=AssertionError("notification forbidden")))

    def status(self, value):
        return stock.trading_time_status(datetime.fromisoformat(value))

    def test_announced_holiday_ranges_are_closed_including_weekdays(self):
        # Expected dates come from the exchanges' 2026 annual notices.
        for start, end, name in (
            ("2026-01-01", "2026-01-03", "元旦"),
            ("2026-02-15", "2026-02-23", "春节"),
            ("2026-04-04", "2026-04-06", "清明节"),
            ("2026-05-01", "2026-05-05", "劳动节"),
            ("2026-06-19", "2026-06-21", "端午节"),
            ("2026-09-25", "2026-09-27", "中秋节"),
            ("2026-10-01", "2026-10-07", "国庆节"),
        ):
            day = datetime.fromisoformat(start).replace(hour=10)
            last = datetime.fromisoformat(end).replace(hour=10)
            while day <= last:
                with self.subTest(day=day):
                    result = stock.trading_time_status(day)
                    self.assertTrue(result["calendar_verified"])
                    self.assertFalse(result["is_trading_day"])
                    self.assertFalse(result["is_trading_time"])
                    self.assertEqual(result["session"], "non_trading_day")
                    self.assertIn(name, result["message"])
                day += timedelta(days=1)

    def test_all_announced_reopening_dates_allow_monitoring(self):
        for date in ("2026-01-05", "2026-02-24", "2026-04-07", "2026-05-06", "2026-06-22", "2026-09-28", "2026-10-08"):
            with self.subTest(date=date):
                result = self.status(date + " 09:30:00")
                self.assertTrue(result["is_trading_day"])
                self.assertTrue(result["is_trading_time"])
                self.assertIsNone(result["next_open"])

    def test_make_up_working_weekends_are_not_trading_days(self):
        for date in ("2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09", "2026-09-20", "2026-10-10"):
            with self.subTest(date=date):
                result = self.status(date + " 10:00:00")
                self.assertFalse(result["is_trading_day"])
                self.assertFalse(result["is_trading_time"])
                self.assertIn("周末", result["message"])

    def test_full_year_matches_announced_regular_day_count(self):
        day = datetime(2026, 1, 1, 10)
        count = 0
        while day.year == 2026:
            count += stock.is_a_share_trading_time(day)
            day += timedelta(days=1)
        self.assertEqual(count, 242)

    def test_national_day_next_open_skips_holiday_weekdays(self):
        for date in ("2026-09-30 15:00:00.000001", "2026-10-04 10:00:00", "2026-10-05 10:00:00", "2026-10-07 14:00:00"):
            with self.subTest(date=date):
                self.assertEqual(self.status(date)["next_open"], "2026-10-08 09:30:00")

    def test_next_open_after_each_holiday_is_the_published_reopening(self):
        for closed, reopening in (
            ("2026-01-01", "2026-01-05"),
            ("2026-02-16", "2026-02-24"),
            ("2026-04-04", "2026-04-07"),
            ("2026-05-01", "2026-05-06"),
            ("2026-06-19", "2026-06-22"),
            ("2026-09-25", "2026-09-28"),
            ("2026-10-01", "2026-10-08"),
        ):
            with self.subTest(closed=closed):
                self.assertEqual(self.status(closed + " 10:00:00")["next_open"], reopening + " 09:30:00")

    def test_morning_start_includes_exact_second_only_after_start(self):
        before = self.status("2026-10-08 09:29:59.999999")
        self.assertFalse(before["is_trading_time"])
        self.assertEqual(before["session"], "pre_open")
        self.assertEqual(before["next_open"], "2026-10-08 09:30:00")
        self.assertTrue(self.status("2026-10-08 09:30:00")["is_trading_time"])

    def test_opening_call_auction_is_outside_monitor_window(self):
        result = self.status("2026-10-08 09:20:00")
        self.assertFalse(result["is_trading_time"])
        self.assertFalse(result["is_continuous_auction"])
        self.assertIn("盯盘开始时间", result["message"])
        self.assertNotIn("尚未开盘", result["message"])

    def test_morning_end_does_not_allow_the_remaining_minute(self):
        self.assertTrue(self.status("2026-10-08 11:30:00")["is_trading_time"])
        for clock in ("11:30:00.000001", "11:30:01", "11:30:59.999999", "12:59:59.999999"):
            with self.subTest(clock=clock):
                result = self.status("2026-10-08 " + clock)
                self.assertFalse(result["is_trading_time"])
                self.assertEqual(result["session"], "lunch_break")
                self.assertEqual(result["next_open"], "2026-10-08 13:00:00")

    def test_afternoon_start_is_exact(self):
        result = self.status("2026-10-08 13:00:00")
        self.assertTrue(result["is_trading_time"])
        self.assertTrue(result["is_continuous_auction"])
        self.assertIsNone(result["next_open"])

    def test_closing_auction_remains_monitored_with_accurate_label(self):
        self.assertTrue(self.status("2026-10-08 14:56:59.999999")["is_continuous_auction"])
        for clock in ("14:57:00", "14:59:59.999999", "15:00:00"):
            with self.subTest(clock=clock):
                result = self.status("2026-10-08 " + clock)
                self.assertTrue(result["is_trading_time"])
                self.assertFalse(result["is_continuous_auction"])
                self.assertEqual(result["session"], "closing_auction")
                self.assertIn("收盘集合竞价", result["message"])
                self.assertIsNone(result["next_open"])

    def test_close_does_not_allow_the_remaining_minute(self):
        for clock in ("15:00:00.000001", "15:00:01", "15:00:59.999999"):
            with self.subTest(clock=clock):
                result = self.status("2026-10-08 " + clock)
                self.assertFalse(result["is_trading_time"])
                self.assertEqual(result["session"], "after_close")
                self.assertEqual(result["next_open"], "2026-10-09 09:30:00")

    def test_friday_close_skips_weekend(self):
        result = self.status("2026-10-09 15:00:00.000001")
        self.assertEqual(result["next_open"], "2026-10-12 09:30:00")

    def test_unsupported_year_is_unknown_and_fail_closed(self):
        for value in ("2025-12-31 10:00:00", "2027-01-04 10:00:00", "2027-01-09 10:00:00"):
            with self.subTest(value=value):
                result = self.status(value)
                self.assertFalse(result["calendar_verified"])
                self.assertEqual(result["calendar_status"], "unverified_year")
                self.assertEqual(result["session"], "calendar_unverified")
                self.assertIsNone(result["is_trading_day"])
                self.assertFalse(result["is_trading_time"])
                self.assertFalse(result["is_continuous_auction"])
                self.assertIsNone(result["next_open"])
                self.assertEqual(result["calendar_sources"], [])
                self.assertIn("请更新日历", result["message"])
                self.assertNotIn("今天是交易日", result["message"])

    def test_verified_year_end_does_not_invent_next_year_open(self):
        result = self.status("2026-12-31 15:00:00.000001")
        self.assertTrue(result["calendar_verified"])
        self.assertTrue(result["is_trading_day"])
        self.assertFalse(result["is_trading_time"])
        self.assertIsNone(result["next_open"])

    def test_aware_utc_is_converted_to_shanghai(self):
        result = stock.trading_time_status(datetime(2026, 10, 8, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(result["now"], "2026-10-08 09:30:00")
        self.assertTrue(result["is_trading_time"])
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertTrue(stock.is_a_share_trading_time(datetime(2026, 10, 8, 1, 30, tzinfo=timezone.utc)))

    def test_aware_date_rollover_uses_local_calendar_day(self):
        result = self.status("2026-10-07T18:00:00+00:00")
        self.assertEqual(result["now"], "2026-10-08 02:00:00")
        self.assertTrue(result["is_trading_day"])
        self.assertEqual(result["next_open"], "2026-10-08 09:30:00")
        rollover = self.status("2026-12-31T16:00:00+00:00")
        self.assertFalse(rollover["calendar_verified"])
        self.assertIsNone(rollover["next_open"])

    def test_default_clock_uses_same_gate(self):
        with mock.patch.object(stock, "_market_now", return_value=datetime(2026, 10, 5, 10)):
            self.assertFalse(stock.is_a_share_trading_time())
            self.assertEqual(stock.trading_time_status()["next_open"], "2026-10-08 09:30:00")

    def test_verified_status_declares_limited_calendar_and_sources(self):
        result = self.status("2026-10-08 10:00:00")
        self.assertEqual(result["calendar_years"], [2026])
        self.assertEqual(result["calendar_status"], "verified")
        self.assertEqual(len(result["calendar_sources"]), 2)
        self.assertIn("临时停市", result["rule"])

    def test_monitor_start_message_does_not_promise_weekday_resume(self):
        result = stock._monitor_start_message("测试盯盘", {"pid": 123}, {"verified": True}, self.status("2026-10-05 10:00:00"))
        self.assertIn("2026-10-08 09:30:00", result)
        self.assertIn("交易所日历", result)
        self.assertNotIn("工作日规则", result)
        self.assertNotIn("暂未纳入", result)

    def test_monitor_start_unknown_calendar_requests_update(self):
        for now in ("2026-12-31 16:00:00", "2027-01-04 10:00:00"):
            with self.subTest(now=now):
                result = stock._monitor_start_message("测试盯盘", {"pid": 123}, {"verified": True}, self.status(now))
                self.assertIn("需更新交易所日历", result)
                self.assertNotIn("将在下一个交易时段恢复", result)
                self.assertNotIn("当前处于交易时段", result)

    def test_status_summary_does_not_call_unknown_next_open_trading(self):
        for now in ("2026-12-31 16:00:00", "2027-01-04 10:00:00"):
            with self.subTest(now=now):
                result = stock.format_monitor_status_summary({"trading_time": self.status(now)})
                self.assertIn("预计恢复扫描：未知", result)
                self.assertNotIn("当前已在交易时段", result)

    def test_status_summary_uses_calendar_for_verified_next_open(self):
        result = stock.format_monitor_status_summary({"trading_time": self.status("2026-10-05 10:00:00")})
        self.assertIn("2026-10-08 09:30:00", result)
        self.assertIn("已收录交易所日历", result)
        self.assertNotIn("工作日规则", result)

    def test_diagnose_explains_unknown_calendar_in_text_and_json(self):
        config = stock._default_watchlist_config()
        config["monitor"].update(enabled=True, mode="normal", market_hours_only=True)
        for now in ("2026-12-31 16:00:00", "2027-01-04 10:00:00"):
            with self.subTest(now=now), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(stock, "_market_now", return_value=datetime.fromisoformat(now)))
                stack.enter_context(mock.patch.object(stock, "load_watchlist_config", return_value=config))
                stack.enter_context(mock.patch.object(stock, "load_monitor_state", return_value={}))
                stack.enter_context(mock.patch.object(stock, "_monitor_status_with_runtime", return_value={"monitor": config["monitor"], "trading_time": self.status(now)}))
                raw = stock.monitor_diagnose(raw_json=True)
                text = stock.monitor_diagnose()["text"]
                self.assertIn("需更新交易所日历", raw["conclusion"])
                self.assertIn("需更新交易所日历", text)
                self.assertEqual(raw["scan_result"]["checked_count"], 0)

    def assert_monitor_gate(self, mode, now):
        config = stock._default_watchlist_config()
        config["monitor"].update(enabled=True, mode=mode, market_hours_only=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(stock, "_market_now", return_value=datetime.fromisoformat(now)))
            stack.enter_context(mock.patch.object(stock, "load_watchlist_config", return_value=config))
            state = stack.enter_context(mock.patch.object(stock, "load_monitor_state", side_effect=AssertionError("state read before calendar gate")))
            quote = stack.enter_context(mock.patch.object(stock, "_monitor_quote", side_effect=AssertionError("quote before calendar gate")))
            inputs = stack.enter_context(mock.patch.object(stock, "_fetch_strategy_inputs", side_effect=AssertionError("strategy fetch before calendar gate")))
            result = stock.build_monitor_alerts_once()
        self.assertEqual(result["checked_count"], 0)
        self.assertEqual(result["alerts_count"], 0)
        self.assertEqual(result["alerts"], [])
        state.assert_not_called()
        quote.assert_not_called()
        inputs.assert_not_called()
        return result

    def test_default_gate_skips_holiday_quotes_in_each_mode(self):
        for mode in ("normal", "strategy"):
            with self.subTest(mode=mode):
                result = self.assert_monitor_gate(mode, "2026-10-05 10:00:00")
                self.assertIn("2026-10-08 09:30:00", result["message"])
                self.assertNotIn("工作日规则", result["message"])

    def test_default_gate_skips_unknown_year_quotes_in_each_mode(self):
        for mode in ("normal", "strategy"):
            with self.subTest(mode=mode):
                result = self.assert_monitor_gate(mode, "2027-01-04 10:00:00")
                self.assertEqual(result["trading_time"]["session"], "calendar_unverified")
                self.assertIn("需更新交易所日历", result["message"])

    def test_default_gate_skips_the_second_after_session_ends(self):
        for mode in ("normal", "strategy"):
            for clock in ("11:30:00.000001", "15:00:00.000001"):
                with self.subTest(mode=mode, clock=clock):
                    self.assert_monitor_gate(mode, "2026-10-08 " + clock)


if __name__ == "__main__":
    unittest.main()
