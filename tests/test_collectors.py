from decimal import Decimal as D

from liquidator.collectors.binance import guess_base, parse_force_order
from liquidator.collectors.bybit import parse_all_liquidation
from liquidator.collectors.okx import parse_liquidation

# Payloads captured live from the exchanges on 2026-10-19 (Bybit's from the v5 docs).
BINANCE_SELL = {"e": "forceOrder", "E": 1792438470395, "o": {
    "s": "ARBUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "15646.7", "p": "0.190895",
    "ap": "0.191993", "X": "FILLED", "l": "1584.9", "z": "15646.7", "T": 1792438469390}}
BINANCE_BUY = {"e": "forceOrder", "E": 1, "o": {
    "s": "XMRUSDT", "S": "BUY", "q": "0.734", "p": "599.37", "ap": "593.57", "z": "0.734",
    "T": 1792438487095}}
OKX_LINEAR = {"data": [{"instId": "RAY-USDT-SWAP", "details": [
    {"bkPx": "1.8713", "posSide": "long", "side": "sell", "sz": "18", "ts": "1792438295906"}]}]}
OKX_INVERSE = {"data": [{"instId": "BTC-USD-SWAP", "details": [
    {"bkPx": "50000", "posSide": "net", "side": "buy", "sz": "5", "ts": "1792438295906"}]}]}
INSTRUMENTS = {
    "RAY-USDT-SWAP": {"ctVal": D("1"), "ctMult": D("1"), "ctValCcy": "RAY"},
    "BTC-USD-SWAP": {"ctVal": D("100"), "ctMult": D("1"), "ctValCcy": "USD"},
}
BYBIT = {"topic": "allLiquidation.BTCUSDT", "type": "snapshot", "ts": 1739502303204, "data": [
    {"T": 1739502302929, "s": "BTCUSDT", "S": "Buy", "v": "0.003", "p": "96000"}]}


def test_binance_usdm_sell_is_long_liquidation():
    liq = parse_force_order(BINANCE_SELL, coinm=False, bases={"ARBUSDT": "ARB"}, contract_size={})
    assert liq.side == "long" and liq.base_asset == "ARB" and liq.exchange == "binance"
    assert liq.price == D("0.191993")
    assert liq.notional_usd == D("15646.7") * D("0.191993")


def test_binance_buy_is_short_liquidation_and_base_fallback():
    liq = parse_force_order(BINANCE_BUY, coinm=False, bases={}, contract_size={})
    assert liq.side == "short" and liq.base_asset == "XMR"


def test_binance_coinm_uses_contract_size():
    msg = {"e": "forceOrder", "o": {"s": "BTCUSD_PERP", "S": "SELL", "q": "10", "p": "50000",
                                    "ap": "50000", "z": "10", "T": 1792438469390}}
    liq = parse_force_order(msg, coinm=True, bases={"BTCUSD_PERP": "BTC"},
                            contract_size={"BTCUSD_PERP": D("100")})
    assert liq.notional_usd == D("1000") and liq.qty == D("0.02")


def test_binance_ignores_other_events():
    assert parse_force_order({"result": None, "id": 1}, coinm=False, bases={}, contract_size={}) is None


def test_guess_base():
    assert guess_base("BTCUSDT") == "BTC" and guess_base("ETHUSD_PERP") == "ETH"
    assert guess_base("1000PEPEUSDC") == "1000PEPE"


def test_okx_linear():
    [liq] = parse_liquidation(OKX_LINEAR, INSTRUMENTS)
    assert liq.side == "long" and liq.base_asset == "RAY"
    assert liq.qty == D("18") and liq.notional_usd == D("18") * D("1.8713")


def test_okx_inverse_net_mode_buy_is_short():
    [liq] = parse_liquidation(OKX_INVERSE, INSTRUMENTS)
    assert liq.side == "short" and liq.notional_usd == D("500") and liq.qty == D("0.01")


def test_okx_unknown_instrument_skipped():
    assert parse_liquidation(OKX_LINEAR, {}) == []


def test_bybit_linear_buy_is_long():
    [liq] = parse_all_liquidation(BYBIT, "linear", {"BTCUSDT": "BTC"})
    assert liq.side == "long" and liq.notional_usd == D("288") and liq.base_asset == "BTC"


def test_bybit_inverse_size_is_usd():
    msg = {"topic": "allLiquidation.BTCUSD", "data": [
        {"T": 1739502302929, "s": "BTCUSD", "S": "Sell", "v": "500", "p": "50000"}]}
    [liq] = parse_all_liquidation(msg, "inverse", {"BTCUSD": "BTC"})
    assert liq.side == "short" and liq.notional_usd == D("500") and liq.qty == D("0.01")


def test_bybit_ignores_acks():
    assert parse_all_liquidation({"success": True, "op": "subscribe"}, "linear", {}) == []
