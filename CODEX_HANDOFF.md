# KolayVeri Portal - Codex Handoff

## Proje
KolayVeri AMI / Sayaç Okuma ve Faturalama Portalı.

## Sunucu
- Proje dizini: /opt/kolayveri_ami
- Portal servis: kolayveri-portal.service
- API servis: kolayveri-api.service
- Portal domain: https://app.kolayveri.com.tr
- API domain: https://api.kolayveri.com.tr
- DB: PostgreSQL kolayveri_db
- DB user: kolayveri_user
- Env dosyası: /opt/kolayveri_ami/.env_portal

## Ana dosyalar
- portal.py
- templates/
- static/style.css
- static/img/kolayveri-logo.png
- api.py
- sync_last_readings_to_pg.py
- period_rollover.py

## Önemli route'lar
- /login
- /dashboard
- /meters
- /period-consumption
- /billing-allocation
- /reactive-consumption
- /monthly-consumption-tracking
- /index-entry
- /admin-data-import
- /period-management

## Kullanıcı tipleri
- admin: tüm tesisleri görür.
- ritim: Ritim İstanbul tesisini görür.
- fundora: Ritim İstanbul altında alt kullanıcıdır; sadece Fundora billing group sayaçlarını ve fatura ekranını görmelidir.

## Son istenen davranış
- Fundora kullanıcısı sadece:
  - Sayaçlar
  - Faturalar
  - Çıkış
  görmeli.
- Fundora sadece billing_group_items içinde bağlı sayaçları görmeli.
- Fundora Faturalar mantığı:
  Ana sayaç tüketimi - süzme sayaçlar toplamı = Diğer Tesisatlar ve Kayıp.
- Normal müşteri/admin ekranları bozulmamalı.

## Dikkat
- .env_portal, DB şifreleri, yedekler ve SQL dump dosyaları GitHub'a gönderilmemeli.
- Canlı DB üzerinde destructive migration yapılmamalı.
- Period rollover apply işlemi dikkatli yapılmalı.
- Her değişiklikten önce dosya backup alınmalı.
- Değişiklik sonrası:
  python -m py_compile portal.py
  systemctl restart kolayveri-portal
  journalctl -u kolayveri-portal -n 120 --no-pager
