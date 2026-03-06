# TrendBreak Bot v2

1dk mum trend takibi + breakout stratejisi. Binance USDT-M Futures (Testnet/Live).

## Strateji
- Son N mumdan kanal (highest high, lowest low) oluşturulur
- Mum kapanışında fiyat kanalı kırarsa → LONG veya SHORT
- Stop-loss: RT tick kontrolü + mum kapanış kontrolü
- Ters sinyal → pozisyon kapatılır
- Cooldown: her trade sonrası bekleme süresi

## Env Variables
- `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- `BINANCE_DEMO=true` (testnet)
- `MODE=live` (live/sim)
- `SYMBOL=BTCUSDT`
- `TRADE_SIZE=100`
- `LEVERAGE=5`
- `TREND_PERIOD=4`
- `BREAK_THRESHOLD=0.05` (%)
- `STOP_LOSS_PCT=0.5` (%)
- `COOLDOWN=15` (saniye)
