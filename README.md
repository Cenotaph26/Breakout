# TrendBreak Bot 🤖

1-dakikalık trend kırılma stratejisi ile çalışan trading bot.

## Strateji

1. Son N mumu analiz ederek trend yönü (yukarı/aşağı/yatay) belirlenir
2. Fiyat trend yüksek seviyesini kırarsa → **LONG** açılır
3. Fiyat trend düşük seviyesini kırarsa → **SHORT** açılır
4. Trend bozulduğunda (fiyat ters tarafa geçtiğinde) → **pozisyon kapanır**
5. Stop loss tetiklendiğinde → **pozisyon kapanır**

## Railway Deploy

### 1. GitHub'a yükle

```bash
git init
git add .
git commit -m "TrendBreak Bot"
git remote add origin https://github.com/KULLANICI/trendbreak-bot.git
git push -u origin main
```

### 2. Railway'e deploy et

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Repo'yu seç
3. Otomatik deploy başlar
4. **Settings → Networking → Generate Domain** ile public URL al

### 3. Kullan

- URL'yi aç → dashboard açılır
- **Simülasyon** modunda test et
- **Binance Live** modunda gerçek veri akışı alır (işlem açmaz, sadece sinyal üretir)

## Ortam Değişkenleri (Opsiyonel)

Railway → Variables kısmına ekle:

| Değişken | Açıklama |
|---|---|
| `PORT` | Otomatik atanır (Railway) |

## Proje Yapısı

```
trendbreak-bot/
├── main.py              # Giriş noktası
├── src/
│   ├── bot.py           # Strateji motoru
│   ├── feed.py          # Binance WS + Simülasyon feed
│   └── server.py        # FastAPI + WebSocket
├── templates/
│   └── index.html       # Dashboard UI
├── requirements.txt
├── railway.toml
├── nixpacks.toml
└── Procfile
```

## API Endpoints

| Method | Path | Açıklama |
|---|---|---|
| GET | `/` | Dashboard |
| POST | `/api/start` | Bot başlat |
| POST | `/api/stop` | Bot durdur |
| GET | `/api/state` | Mevcut durum |
| GET | `/api/health` | Sağlık kontrolü |
| WS | `/ws` | Gerçek zamanlı veri |

## Yerel Çalıştırma

```bash
pip install -r requirements.txt
python main.py
# → http://localhost:8000
```

## ⚠️ Uyarı

Bu bot eğitim amaçlıdır. Gerçek para ile kullanmadan önce:
- Simülasyon modunda kapsamlı test yapın
- Risk yönetimini dikkatli ayarlayın
- Geçmiş performans gelecek sonuçları garantilemez
