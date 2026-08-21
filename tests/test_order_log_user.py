"""Order-audit attribution: every ``nano_order`` line carries the
signed-in NanoPulse username.

Before this, ``nano_order.log`` recorded the connection label and the IB
account but never the person — and several users can share a connection
(both ``alex`` and ``admin`` have ``Q-ib`` configured), so the log alone
could not answer "who placed this?".  The username is injected by a
handler-side filter rather than threaded through the 48 ``order_log``
call sites, so lines added later are stamped for free.

The stamp says which SESSION wrote the line.  For the app's own actions
that is the person who triggered them; ORDER-SNAPSHOT / ORDER-STATUS
lines are replays of what TWS pushed (``reqAllOpenOrders`` returns every
client's orders), so there it is only whoever was watching.
"""
from __future__ import annotations

import logging

import pytest

from src.utils import logger as lg


@pytest.fixture(autouse=True)
def _reset_user():
    """Never leak a username into another test — the stamp is process-wide."""
    lg.set_log_user(None)
    yield
    lg.set_log_user(None)


@pytest.fixture
def rendered():
    """Render one ``order_log`` call through the real order formatter and
    filter, capturing the finished line."""
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    h = _Capture()
    h.setFormatter(logging.Formatter(lg._ORDER_FMT, datefmt=lg._DATE_FMT))
    h.addFilter(lg._SessionUserFilter())
    lg.order_log.addHandler(h)
    # setup_logging() isn't called in the suite (it writes to the real
    # data/logs), so the logger would otherwise inherit root's WARNING.
    prev = lg.order_log.level
    lg.order_log.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        lg.order_log.removeHandler(h)
        lg.order_log.setLevel(prev)


class TestSetLogUser:
    def test_default_is_the_signed_out_placeholder(self):
        assert lg.get_log_user() == lg.NO_USER == "-"

    def test_set_and_read_back(self):
        lg.set_log_user("alex")
        assert lg.get_log_user() == "alex"

    def test_none_clears_to_placeholder(self):
        lg.set_log_user("alex")
        lg.set_log_user(None)
        assert lg.get_log_user() == "-"

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_usernames_fall_back(self, raw):
        """An empty name must not render as an empty column — the field
        would silently disappear from the audit trail."""
        lg.set_log_user(raw)
        assert lg.get_log_user() == "-"

    def test_surrounding_whitespace_stripped(self):
        lg.set_log_user("  alex  ")
        assert lg.get_log_user() == "alex"


class TestRenderedLine:
    def test_username_appears_in_the_line(self, rendered):
        lg.set_log_user("alex")
        lg.order_log.info("[%s] ORDER-SUBMIT  %s", "Q-ib", "MNQU6 BUY 1")
        assert len(rendered) == 1
        assert "alex" in rendered[0]
        assert "ORDER-SUBMIT" in rendered[0]
        assert "[Q-ib]" in rendered[0]

    def test_signed_out_lines_carry_the_placeholder(self, rendered):
        lg.order_log.info("[%s] ORDER-STATUS  late cancel", "Q-ib")
        assert " -  " in rendered[0] or rendered[0].split()[2] == "-"

    def test_stamp_follows_the_current_user(self, rendered):
        """Sign-in / sign-out / sign-in: each line takes the name that was
        set when it was logged, not the latest one."""
        lg.set_log_user("alex")
        lg.order_log.info("first")
        lg.set_log_user(None)
        lg.order_log.info("second")
        lg.set_log_user("demo")
        lg.order_log.info("third")
        assert "alex" in rendered[0]
        assert "alex" not in rendered[1] and "demo" not in rendered[1]
        assert "demo" in rendered[2]

    def test_message_survives_intact(self, rendered):
        """The user column is prepended — the existing line body (which
        other tooling greps) must be byte-identical."""
        lg.set_log_user("alex")
        body = ("[Q-ib] ORDER-SNAPSHOT  U2222222_p1695212004 | status=Filled "
                "symbol=VOO action=BUY qty=1 fill=707.77 (completed)")
        lg.order_log.info("%s", body)
        assert rendered[0].endswith(body)

    def test_filter_never_drops_a_record(self, rendered):
        """It decorates, it does not gate — a filter returning False here
        would silently lose orders from the audit trail."""
        for i in range(5):
            lg.order_log.info("line %d", i)
        assert len(rendered) == 5


class TestFilterUnit:
    def test_stamps_the_record_attribute(self):
        lg.set_log_user("alex")
        rec = logging.LogRecord("nano_order", logging.INFO, __file__, 1,
                                "msg", None, None)
        assert lg._SessionUserFilter().filter(rec) is True
        assert rec.user == "alex"

    def test_format_string_declares_the_field(self):
        assert "%(user)" in lg._ORDER_FMT
        # tws / root formats are untouched — no filter feeds them.
        assert "%(user)" not in lg._TWS_FMT
        assert "%(user)" not in lg._FILE_FMT
        assert "%(user)" not in lg._CONSOLE_FMT


class TestHandlerWiring:
    def test_configure_attaches_filters_to_the_handler(self, tmp_path):
        """Handler-side, not logger-side: a logger-side filter is skipped
        for records arriving from a child logger, and the formatter would
        then raise on the missing ``user`` attribute."""
        lgr = logging.getLogger("nano_order_test_wiring")
        lgr.handlers.clear()
        filt = lg._SessionUserFilter()
        lg._configure_dedicated_logger(
            lgr, tmp_path / "o.log", lg._ORDER_FMT,
            file_level=logging.INFO, filters=(filt,))
        try:
            assert filt in lgr.handlers[0].filters
            assert filt not in lgr.filters
            lg.set_log_user("alex")
            lgr.info("[%s] ORDER-SUBMIT  x", "Q-ib")
            lgr.handlers[0].flush()
            assert "alex" in (tmp_path / "o.log").read_text()
        finally:
            for h in lgr.handlers:
                h.close()
            lgr.handlers.clear()

    def test_child_logger_record_still_renders(self, tmp_path):
        lgr = logging.getLogger("nano_order_test_child")
        lgr.handlers.clear()
        lg._configure_dedicated_logger(
            lgr, tmp_path / "o.log", lg._ORDER_FMT,
            file_level=logging.INFO, filters=(lg._SessionUserFilter(),))
        try:
            lg.set_log_user("demo")
            logging.getLogger("nano_order_test_child.sub").info("child line")
            lgr.handlers[0].flush()
            assert "demo" in (tmp_path / "o.log").read_text()
        finally:
            for h in lgr.handlers:
                h.close()
            lgr.handlers.clear()

    def test_no_filters_argument_leaves_the_handler_bare(self, tmp_path):
        """tws_log passes no filters and must stay unaffected."""
        lgr = logging.getLogger("nano_tws_test_wiring")
        lgr.handlers.clear()
        lg._configure_dedicated_logger(
            lgr, tmp_path / "t.log", lg._TWS_FMT, file_level=logging.INFO)
        try:
            assert lgr.handlers[0].filters == []
        finally:
            for h in lgr.handlers:
                h.close()
            lgr.handlers.clear()


class TestAppControllerWiring:
    """The stamp is set on sign-in and cleared on sign-out."""

    def test_authenticated_sets_it_before_the_window_is_built(self):
        import inspect

        import main
        src = inspect.getsource(main.AppController._on_authenticated)
        assert "set_log_user(user.username)" in src
        # Ordering matters: a MainWindow can place an order as soon as it
        # exists, so the stamp has to be set first.
        assert src.index("set_log_user") < src.index("MainWindow(")

    def test_sign_out_clears_it(self):
        import inspect

        import main
        assert "set_log_user(None)" in inspect.getsource(
            main.AppController._on_sign_out)
