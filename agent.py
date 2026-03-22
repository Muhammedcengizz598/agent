#!/usr/bin/env python3
"""
Cherry Agent — Termux için Cihaz Yönetim Ajanı
Bu script Termux'ta çalıştırılır ve sunucuya WebSocket ile bağlanır.

Kurulum:
    pkg install python termux-api
    pip install websockets requests
    python agent.py

İlk çalıştırmada otomatik olarak sunucuya kayıt olur.
Sunucu kapatılıp açılsa bile agent tekrar bağlanır.
"""

import asyncio
import json
import os
import sys
import uuid
import time
import base64
import subprocess
import signal
import logging
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[!] websockets modülü bulunamadı. Kurmak için: pip install websockets")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[!] requests modülü bulunamadı. Kurmak için: pip install requests")
    sys.exit(1)

# ===== YAPILANDIRMA =====
# Sunucu adresi — kendi bilgisayarınızın IP'sini girin
# Yerel ağ için: 192.168.x.x
# ngrok için: xxxx.ngrok.io
SERVER_HOST = "192.168.1.4"  # Laptop IP adresi
SERVER_PORT = 8000
USE_SSL = False  # ngrok kullanıyorsanız True yapın

# Otomatik veri gönderme aralıkları (saniye)
GPS_INTERVAL = 60  # Her 60 saniyede GPS gönder
SENSOR_INTERVAL = 120  # Her 120 saniyede sensör verisi gönder
RECONNECT_MIN_DELAY = 2  # İlk yeniden bağlanma bekleme süresi
RECONNECT_MAX_DELAY = 60  # Maksimum yeniden bağlanma bekleme süresi

# ===== SABİTLER =====
CONFIG_FILE = os.path.expanduser("~/.cherry_agent.json")
LOG_FILE = os.path.expanduser("~/.cherry_agent.log")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ]
)
logger = logging.getLogger('cherry_agent')


class CherryAgent:
    """Ana agent sınıfı."""

    def __init__(self):
        self.config = self.load_config()
        self.ws = None
        self.running = True
        self.authenticated = False
        self.reconnect_delay = RECONNECT_MIN_DELAY

    # ===== CONFIG =====
    def load_config(self):
        """Kayıtlı yapılandırmayı yükle."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                logger.info(f"Yapılandırma yüklendi: {CONFIG_FILE}")
                return config
            except Exception as e:
                logger.error(f"Yapılandırma yüklenemedi: {e}")
        return {}

    def save_config(self):
        """Yapılandırmayı kaydet."""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
            logger.info(f"Yapılandırma kaydedildi: {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Yapılandırma kaydedilemedi: {e}")

    def get_device_uuid(self):
        """Benzersiz cihaz UUID'si al veya oluştur."""
        if 'device_uuid' not in self.config:
            self.config['device_uuid'] = str(uuid.uuid4())
            self.save_config()
        return self.config['device_uuid']

    # ===== CİHAZ BİLGİLERİ =====
    def get_device_info(self):
        """Cihaz bilgilerini topla."""
        info = {
            'platform': 'Android',
            'model': 'Bilinmiyor',
            'os_version': '',
            'name': 'Android Cihaz',
        }

        try:
            # Model adı
            result = subprocess.run(
                ['getprop', 'ro.product.model'],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                info['model'] = result.stdout.strip()
                info['name'] = result.stdout.strip()
        except Exception:
            pass

        try:
            # Android versiyonu
            result = subprocess.run(
                ['getprop', 'ro.build.version.release'],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                info['os_version'] = f"Android {result.stdout.strip()}"
        except Exception:
            pass

        try:
            # Marka
            result = subprocess.run(
                ['getprop', 'ro.product.brand'],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                brand = result.stdout.strip().capitalize()
                info['name'] = f"{brand} {info['model']}"
        except Exception:
            pass

        return info

    # ===== KAYIT =====
    def register_device(self, force=False):
        """Sunucuya ilk kayıt veya yeniden kayıt."""
        if 'token' in self.config and not force:
            logger.info("Cihaz zaten kayıtlı, mevcut token kullanılıyor.")
            return True

        # Force durumunda eski token'ı sil
        if force:
            self.config.pop('token', None)
            logger.info("Yeniden kayıt yapılıyor (eski token silindi)...")

        device_info = self.get_device_info()
        device_uuid = self.get_device_uuid()

        protocol = 'https' if USE_SSL else 'http'
        url = f"{protocol}://{SERVER_HOST}:{SERVER_PORT}/api/register/"

        payload = {
            'device_uuid': device_uuid,
            'platform': device_info['platform'],
            'model': device_info['model'],
            'os_version': device_info['os_version'],
            'name': device_info['name'],
        }

        for attempt in range(10):
            try:
                logger.info(f"Sunucuya kayıt olunuyor... (deneme {attempt + 1})")
                response = requests.post(url, json=payload, timeout=15)
                data = response.json()

                if data.get('status') == 'ok':
                    self.config['token'] = data['token']
                    self.config['device_uuid'] = data['device_uuid']
                    self.save_config()
                    logger.info(f"✅ Kayıt başarılı! Token alındı.")
                    return True
                else:
                    logger.error(f"Kayıt hatası: {data}")
            except requests.exceptions.ConnectionError:
                logger.warning(f"Sunucuya bağlanılamadı. {2 ** attempt} saniye sonra tekrar denenecek...")
                time.sleep(min(2 ** attempt, 60))
            except Exception as e:
                logger.error(f"Kayıt hatası: {e}")
                time.sleep(min(2 ** attempt, 60))

        logger.error("Kayıt başarısız oldu. Sunucunun çalıştığından emin olun.")
        return False

    # ===== WEBSOCKET =====
    async def connect(self):
        """WebSocket bağlantısı kur ve mesajları dinle."""
        ws_protocol = 'wss' if USE_SSL else 'ws'
        device_uuid = self.get_device_uuid()
        url = f"{ws_protocol}://{SERVER_HOST}:{SERVER_PORT}/ws/device/{device_uuid}/"

        while self.running:
            try:
                logger.info(f"WebSocket bağlantısı kuruluyor: {url}")
                async with websockets.connect(
                    url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=100 * 1024 * 1024,  # 100MB max message
                ) as ws:
                    self.ws = ws
                    self.reconnect_delay = RECONNECT_MIN_DELAY  # Reset delay
                    logger.info("✅ WebSocket bağlantısı kuruldu!")

                    # Authenticate
                    await self.authenticate()

                    # Auth timeout check
                    asyncio.create_task(self.wait_for_auth())

                    # Start background tasks
                    tasks = [
                        asyncio.create_task(self.listen()),
                        asyncio.create_task(self.periodic_gps()),
                        asyncio.create_task(self.periodic_sensor()),
                    ]

                    # Wait until any task completes (usually listen on disconnect)
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )

                    # Cancel remaining tasks
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

            except (websockets.exceptions.ConnectionClosed, 
                    websockets.exceptions.InvalidURI,
                    OSError, ConnectionRefusedError) as e:
                logger.warning(f"Bağlantı kesildi: {e}")
            except Exception as e:
                logger.error(f"Beklenmeyen hata: {e}")
            finally:
                self.ws = None
                self.authenticated = False

            if self.running:
                logger.info(f"⏳ {self.reconnect_delay} saniye sonra yeniden bağlanılacak...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, RECONNECT_MAX_DELAY)

                # URL'yi güncelle (UUID değişmiş olabilir)
                device_uuid = self.get_device_uuid()
                url = f"{ws_protocol}://{SERVER_HOST}:{SERVER_PORT}/ws/device/{device_uuid}/"

    async def authenticate(self):
        """Token ile kimlik doğrulama."""
        auth_msg = {
            'type': 'auth',
            'token': self.config.get('token', ''),
            'device_uuid': self.config.get('device_uuid', ''),
        }
        await self.ws.send(json.dumps(auth_msg))
        logger.info("Kimlik doğrulama mesajı gönderildi...")

    async def wait_for_auth(self):
        """Auth sonucunu bekle — 10 sn içinde auth olmazsa yeniden kayıt ol."""
        await asyncio.sleep(10)
        if not self.authenticated:
            logger.warning("⚠️ Auth 10 saniyede tamamlanmadı. Yeniden kayıt yapılıyor...")
            self.register_device(force=True)
            # Bağlantıyı kapat, yeni token ile tekrar bağlanacak
            if self.ws:
                await self.ws.close()

    async def listen(self):
        """Gelen mesajları dinle ve işle."""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    await self.handle_message(data)
                except json.JSONDecodeError:
                    logger.error(f"Geçersiz JSON: {message[:100]}")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket bağlantısı kapatıldı.")

    async def handle_message(self, data):
        """Gelen mesajları türüne göre işle."""
        msg_type = data.get('type', '')
        command_id = data.get('command_id')

        logger.info(f"📩 Mesaj alındı: {msg_type}")

        handlers = {
            'auth_result': self.handle_auth_result,
            'get_location': self.handle_get_location,
            'get_screenshot': self.handle_get_screenshot,
            'get_sensor': self.handle_get_sensor,
            'touch': self.handle_touch,
            'swipe': self.handle_swipe,
            'type_text': self.handle_type_text,
            'send_file': self.handle_receive_file,
            'get_file': self.handle_send_file,
            'send_text': self.handle_send_text,
            'lock_screen': self.handle_lock_screen,
            'get_app_list': self.handle_app_list,
            'alarm': self.handle_alarm,
            'key_home': self.handle_key_home,
            'key_back': self.handle_key_back,
            'key_recent': self.handle_key_recent,
            'volume_set': self.handle_volume_set,
            'shell': self.handle_shell,
        }

        handler = handlers.get(msg_type)
        if handler:
            try:
                await handler(data, command_id)
            except Exception as e:
                logger.error(f"Komut işleme hatası ({msg_type}): {e}")
                if command_id:
                    await self.send_cmd_result(command_id, 'error', error=str(e))
        elif msg_type != 'auth_result':
            logger.warning(f"Bilinmeyen mesaj türü: {msg_type}")

    async def send_json(self, data):
        """JSON mesaj gönder."""
        if self.ws and self.ws.open:
            try:
                await self.ws.send(json.dumps(data))
            except Exception as e:
                logger.error(f"Mesaj gönderilemedi: {e}")

    async def send_cmd_result(self, command_id, status='done', data=None, error=''):
        """Komut sonucu gönder."""
        await self.send_json({
            'type': 'cmd_result',
            'command_id': command_id,
            'status': status,
            'data': data or {},
            'error': error,
        })

    # ===== MESAJ İŞLEYİCİLER =====

    async def handle_auth_result(self, data, command_id=None):
        if data.get('status') == 'ok':
            self.authenticated = True
            logger.info(f"✅ Kimlik doğrulama başarılı: {data.get('message')}")
            # İlk bağlantıda sensör verisi ve konum gönder
            await asyncio.sleep(1)
            await self.send_sensor_data()
            await self.send_location()
        else:
            logger.error(f"❌ Kimlik doğrulama başarısız: {data.get('message')}")
            self.authenticated = False
            # Yeniden kayıt ol ve bağlantıyı kapat
            logger.info("🔄 Yeniden kayıt yapılıyor...")
            self.register_device(force=True)
            if self.ws:
                await self.ws.close()

    async def handle_get_location(self, data, command_id=None):
        """GPS konumu al ve gönder."""
        result = await self.send_location()
        if command_id:
            await self.send_cmd_result(command_id, 'done' if result else 'error',
                                        error='' if result else 'Konum alınamadı')

    async def send_location(self):
        """Termux API ile GPS konumu al ve gönder."""
        try:
            logger.info("📍 GPS konumu alınıyor...")
            result = subprocess.run(
                ['termux-location', '-p', 'gps', '-r', 'last'],
                capture_output=True, text=True, timeout=30
            )

            if result.stdout.strip():
                loc = json.loads(result.stdout)
                await self.send_json({
                    'type': 'location',
                    'lat': loc.get('latitude'),
                    'lng': loc.get('longitude'),
                    'accuracy': loc.get('accuracy'),
                })
                logger.info(f"📍 Konum gönderildi: {loc.get('latitude')}, {loc.get('longitude')}")
                return True
            else:
                # Fallback: network provider
                result = subprocess.run(
                    ['termux-location', '-p', 'network', '-r', 'last'],
                    capture_output=True, text=True, timeout=30
                )
                if result.stdout.strip():
                    loc = json.loads(result.stdout)
                    await self.send_json({
                        'type': 'location',
                        'lat': loc.get('latitude'),
                        'lng': loc.get('longitude'),
                        'accuracy': loc.get('accuracy'),
                    })
                    logger.info(f"📍 Konum gönderildi (network): {loc.get('latitude')}, {loc.get('longitude')}")
                    return True
                logger.warning("GPS verisi boş")
                return False
        except subprocess.TimeoutExpired:
            logger.warning("GPS zaman aşımı")
            return False
        except Exception as e:
            logger.error(f"GPS hatası: {e}")
            return False

    async def handle_get_screenshot(self, data, command_id=None):
        """Ekran görüntüsü al ve gönder."""
        try:
            logger.info("📸 Ekran görüntüsü alınıyor...")
            screenshot_path = '/tmp/cherry_screenshot.png'

            # Önce screencap dene
            result = subprocess.run(
                ['screencap', '-p', screenshot_path],
                capture_output=True, timeout=10
            )

            if os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0:
                with open(screenshot_path, 'rb') as f:
                    image_data = base64.b64encode(f.read()).decode()

                # Boyut bilgisi (basit)
                await self.send_json({
                    'type': 'screenshot',
                    'data': image_data,
                    'width': 1080,
                    'height': 1920,
                })
                logger.info("📸 Ekran görüntüsü gönderildi")

                os.remove(screenshot_path)

                if command_id:
                    await self.send_cmd_result(command_id, 'done')
                return
            else:
                logger.warning("screencap başarısız oldu")
                if command_id:
                    await self.send_cmd_result(command_id, 'error', error='Ekran görüntüsü alınamadı. Root izni gerekebilir.')
        except Exception as e:
            logger.error(f"Screenshot hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_get_sensor(self, data, command_id=None):
        """Sensör verisi gönder."""
        await self.send_sensor_data()
        if command_id:
            await self.send_cmd_result(command_id, 'done')

    async def send_sensor_data(self):
        """Sensör verilerini topla ve gönder."""
        sensor = {
            'type': 'sensor',
            'battery': None,
            'is_charging': False,
            'wifi': '',
            'signal': None,
            'ip': '',
            'storage_free': None,
            'storage_total': None,
            'ram_total': None,
        }

        # Pil bilgisi
        try:
            result = subprocess.run(
                ['termux-battery-status'],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                bat = json.loads(result.stdout)
                sensor['battery'] = bat.get('percentage')
                sensor['is_charging'] = bat.get('status') == 'CHARGING'
        except Exception as e:
            logger.error(f"Pil bilgisi alınamadı: {e}")

        # WiFi bilgisi
        try:
            result = subprocess.run(
                ['termux-wifi-connectioninfo'],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                wifi = json.loads(result.stdout)
                sensor['wifi'] = wifi.get('ssid', '')
                sensor['signal'] = wifi.get('rssi')
                sensor['ip'] = wifi.get('ip', '')
        except Exception as e:
            logger.error(f"WiFi bilgisi alınamadı: {e}")

        # Depolama
        try:
            result = subprocess.run(
                ['df', '-h', '/data'],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split()
                if len(parts) >= 4:
                    def parse_size(s):
                        s = s.upper()
                        if s.endswith('G'):
                            return float(s[:-1])
                        elif s.endswith('M'):
                            return float(s[:-1]) / 1024
                        elif s.endswith('T'):
                            return float(s[:-1]) * 1024
                        return 0

                    sensor['storage_total'] = parse_size(parts[1])
                    sensor['storage_free'] = parse_size(parts[3])
        except Exception as e:
            logger.error(f"Depolama bilgisi alınamadı: {e}")

        # RAM
        try:
            result = subprocess.run(
                ['cat', '/proc/meminfo'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split('\n'):
                if line.startswith('MemTotal:'):
                    mem_kb = int(line.split()[1])
                    sensor['ram_total'] = mem_kb / 1024  # MB
                    break
        except Exception as e:
            logger.error(f"RAM bilgisi alınamadı: {e}")

        await self.send_json(sensor)
        logger.info(f"📊 Sensör verisi gönderildi (Pil: {sensor['battery']}%)")

    async def handle_touch(self, data, command_id=None):
        """Dokunma simülasyonu."""
        x = data.get('x', 0)
        y = data.get('y', 0)
        try:
            subprocess.run(['input', 'tap', str(x), str(y)], timeout=5)
            logger.info(f"👆 Dokunma: ({x}, {y})")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            logger.error(f"Dokunma hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_swipe(self, data, command_id=None):
        """Kaydırma simülasyonu."""
        x1 = data.get('x1', 0)
        y1 = data.get('y1', 0)
        x2 = data.get('x2', 0)
        y2 = data.get('y2', 0)
        duration = data.get('duration', 300)
        try:
            subprocess.run(['input', 'swipe', str(x1), str(y1), str(x2), str(y2), str(duration)], timeout=5)
            logger.info(f"👆 Kaydırma: ({x1},{y1}) → ({x2},{y2})")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            logger.error(f"Kaydırma hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_type_text(self, data, command_id=None):
        """Metin yazma simülasyonu."""
        text = data.get('text', '')
        try:
            subprocess.run(['input', 'text', text], timeout=10)
            logger.info(f"⌨️ Metin yazıldı: {text[:50]}")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            logger.error(f"Metin yazma hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_receive_file(self, data, command_id=None):
        """Sunucudan gelen dosyayı kaydet."""
        filename = data.get('filename', 'received_file')
        file_data = data.get('data', '')
        try:
            save_dir = os.path.expanduser('~/storage/downloads/cherry/')
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)

            with open(save_path, 'wb') as f:
                f.write(base64.b64decode(file_data))

            logger.info(f"📥 Dosya kaydedildi: {save_path}")
            if command_id:
                await self.send_cmd_result(command_id, 'done', data={'path': save_path})
        except Exception as e:
            logger.error(f"Dosya kaydetme hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_send_file(self, data, command_id=None):
        """Cihazdan sunucuya dosya gönder."""
        file_path = data.get('path', '')
        try:
            if not os.path.exists(file_path):
                if command_id:
                    await self.send_cmd_result(command_id, 'error', error='Dosya bulunamadı')
                return

            with open(file_path, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode()

            filename = os.path.basename(file_path)
            await self.send_json({
                'type': 'file_data',
                'filename': filename,
                'data': file_data,
            })
            logger.info(f"📤 Dosya gönderildi: {filename}")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            logger.error(f"Dosya gönderme hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_send_text(self, data, command_id=None):
        """Gelen metni logla."""
        text = data.get('text', '')
        logger.info(f"💬 Metin alındı: {text}")
        if command_id:
            await self.send_cmd_result(command_id, 'done')

    async def handle_lock_screen(self, data, command_id=None):
        """Ekranı kilitle."""
        try:
            subprocess.run(['input', 'keyevent', '26'], timeout=5)  # KEYCODE_POWER
            logger.info("🔒 Ekran kilitlendi")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            logger.error(f"Kilitleme hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_app_list(self, data, command_id=None):
        """Yüklü uygulamaları listele."""
        try:
            result = subprocess.run(
                ['pm', 'list', 'packages', '-3'],
                capture_output=True, text=True, timeout=15
            )
            apps = []
            for line in result.stdout.strip().split('\n'):
                if line.startswith('package:'):
                    apps.append(line.replace('package:', ''))
            
            logger.info(f"📱 {len(apps)} uygulama listelendi")
            if command_id:
                await self.send_cmd_result(command_id, 'done', data={'apps': sorted(apps)})
        except Exception as e:
            logger.error(f"Uygulama listesi hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_alarm(self, data, command_id=None):
        """Alarm çal (cihazı bulmak için)."""
        try:
            # Sesi maksimuma aç ve alarm çal
            subprocess.run(['termux-volume', 'music', '15'], timeout=5)
            subprocess.run(
                ['termux-media-player', 'play',
                 '/system/media/audio/ringtones/Default.ogg'],
                timeout=5
            )
            logger.info("🔔 Alarm başlatıldı!")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            logger.error(f"Alarm hatası: {e}")
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_key_home(self, data, command_id=None):
        """Ana ekran tuşu."""
        try:
            subprocess.run(['input', 'keyevent', '3'], timeout=5)
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_key_back(self, data, command_id=None):
        """Geri tuşu."""
        try:
            subprocess.run(['input', 'keyevent', '4'], timeout=5)
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_key_recent(self, data, command_id=None):
        """Son uygulamalar tuşu."""
        try:
            subprocess.run(['input', 'keyevent', '187'], timeout=5)
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_volume_set(self, data, command_id=None):
        """Ses seviyesini ayarla."""
        level = data.get('level', 7)
        try:
            subprocess.run(['termux-volume', 'music', str(level)], timeout=5)
            logger.info(f"🔊 Ses seviyesi: {level}")
            if command_id:
                await self.send_cmd_result(command_id, 'done')
        except Exception as e:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    async def handle_shell(self, data, command_id=None):
        """Shell komutu çalıştır."""
        cmd = data.get('command', '')
        if not cmd:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error='Komut boş')
            return
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            output = result.stdout + result.stderr
            logger.info(f"💻 Shell: {cmd[:50]} → {output[:100]}")
            if command_id:
                await self.send_cmd_result(command_id, 'done', data={
                    'output': output,
                    'return_code': result.returncode
                })
        except subprocess.TimeoutExpired:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error='Komut zaman aşımı (30s)')
        except Exception as e:
            if command_id:
                await self.send_cmd_result(command_id, 'error', error=str(e))

    # ===== PERİYODİK GÖREVLER =====

    async def periodic_gps(self):
        """Periyodik GPS gönderimi."""
        while self.running:
            await asyncio.sleep(GPS_INTERVAL)
            if self.ws and not self.ws.closed and self.authenticated:
                await self.send_location()

    async def periodic_sensor(self):
        """Periyodik sensör verisi gönderimi."""
        while self.running:
            await asyncio.sleep(SENSOR_INTERVAL)
            if self.ws and not self.ws.closed and self.authenticated:
                await self.send_sensor_data()

    # ===== ANA DÖNGÜ =====
    async def run(self):
        """Agent'ı başlat."""
        print("""
╔══════════════════════════════════════════╗
║         🍒 Cherry Agent v1.0            ║
║      Cihaz Yönetim Sistemi Ajanı        ║
╠══════════════════════════════════════════╣
║  Sunucu: {:<31s} ║
║  Port:   {:<31s} ║
╚══════════════════════════════════════════╝
        """.format(SERVER_HOST, str(SERVER_PORT)))

        # Register if needed
        if not self.register_device():
            logger.error("Kayıt başarısız. Çıkılıyor.")
            return

        logger.info(f"Cihaz UUID: {self.config['device_uuid']}")
        logger.info("WebSocket bağlantısı başlatılıyor...")

        # Start WebSocket connection (infinite loop with reconnect)
        await self.connect()


def main():
    """Entry point."""
    agent = CherryAgent()

    # Handle SIGINT/SIGTERM gracefully
    def signal_handler(sig, frame):
        logger.info("Çıkış sinyali alındı. Kapatılıyor...")
        agent.running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        logger.info("Agent kapatıldı.")


if __name__ == '__main__':
    main()
