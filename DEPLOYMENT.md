# OGN FlightMonitor - Deployment auf Hetzner Cloud

Schritt-fuer-Schritt Anleitung: Server aufsetzen, App deployen,
SSL mit Let's Encrypt unter https://flight-monitor.de

## Voraussetzungen

- Hetzner Cloud Account (https://console.hetzner.cloud)
- Domain `flight-monitor.de` mit Zugriff auf DNS-Einstellungen
- Lokaler Rechner mit SSH-Key (`ssh-keygen -t ed25519` falls noch keiner existiert)

---

## 1. Hetzner Server erstellen

1. Hetzner Cloud Console oeffnen -> "Server erstellen"
2. Einstellungen:
   - **Standort**: Falkenstein (fsn1) oder Nuernberg (nbg1)
   - **Image**: Ubuntu 24.04
   - **Typ**: CX22 (2 vCPU, 4 GB RAM, 40 GB SSD) - reicht fuer Anfang
   - **SSH-Key**: Deinen oeffentlichen Key hinzufuegen
   - **Firewall**: Neue Firewall erstellen mit folgenden Regeln:
     - TCP 22 (SSH) - nur deine IP oder 0.0.0.0/0
     - TCP 80 (HTTP) - 0.0.0.0/0
     - TCP 443 (HTTPS) - 0.0.0.0/0
   - **Name**: `flight-monitor`
3. Server erstellen -> IP-Adresse notieren (z.B. `65.108.xxx.xxx`)

## 2. DNS konfigurieren

Bei deinem Domain-Registrar (z.B. Hetzner DNS, Cloudflare, INWX):

```
flight-monitor.de.       A     65.108.xxx.xxx
www.flight-monitor.de.   A     65.108.xxx.xxx
```

> DNS-Propagierung kann 5-30 Minuten dauern.
> Pruefen mit: `dig flight-monitor.de +short`

## 3. Server einrichten

```bash
ssh root@65.108.xxx.xxx
```

### 3.1 System aktualisieren

```bash
apt update && apt upgrade -y
```

### 3.2 Docker installieren

```bash
# Docker GPG Key + Repository
curl -fsSL https://get.docker.com | sh

# Docker Compose ist in Docker Engine enthalten (docker compose)
docker --version
docker compose version
```

### 3.3 Firewall (ufw) einrichten

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

### 3.4 Deploy-User erstellen (optional, empfohlen)

```bash
adduser --disabled-password deploy
usermod -aG docker deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
```

Ab hier als `deploy`-User oder weiter als `root`.

## 4. Projekt auf den Server bringen

### Option A: Direkt von GitHub klonen

```bash
cd /opt
git clone https://github.com/achimace/ogn_monitor.git
cd ogn_monitor
```

### Option B: Per rsync vom lokalen Rechner

```bash
# Lokal ausfuehren:
rsync -avz --exclude node_modules --exclude .git --exclude __pycache__ \
  ./ root@65.108.xxx.xxx:/opt/ogn_monitor/
```

## 5. Environment konfigurieren

```bash
cd /opt/ogn_monitor
cp .env.example .env
```

`.env` editieren:

```bash
nano .env
```

```env
# Sichere Passwoerter generieren:
#   openssl rand -hex 32

DB_USER=ogn_monitor
DB_PASSWORD=<openssl rand -hex 32>
DB_PORT=5432

JWT_SECRET=<openssl rand -hex 64>

OGN_CALLSIGN=FLIGHTMON

BASE_URL=https://flight-monitor.de
LOG_LEVEL=info
API_WORKERS=4

HTTP_PORT=80
HTTPS_PORT=443
```

Passwoerter direkt generieren:

```bash
sed -i "s/CHANGE_ME_secure_password_here/$(openssl rand -hex 32)/" .env
sed -i "s/CHANGE_ME_at_least_64_random_characters_here/$(openssl rand -hex 64)/" .env
```

## 6. SSL mit Let's Encrypt einrichten

### 6.1 Certbot installieren

```bash
apt install -y certbot
```

### 6.2 Erstes Zertifikat holen (Standalone-Modus)

Nginx darf noch NICHT laufen (Port 80 muss frei sein):

```bash
certbot certonly --standalone \
  -d flight-monitor.de \
  -d www.flight-monitor.de \
  --agree-tos \
  --email admin@flight-monitor.de \
  --non-interactive
```

Zertifikate liegen dann unter:
- `/etc/letsencrypt/live/flight-monitor.de/fullchain.pem`
- `/etc/letsencrypt/live/flight-monitor.de/privkey.pem`

### 6.3 Produktions-Nginx-Config aktivieren

Die Prod-Config mit SSL liegt bereits fertig im Repo als `nginx/nginx.prod.conf`.
Ersetze die Dev-Config damit:

```bash
cd /opt/ogn_monitor
cp nginx/nginx.prod.conf nginx/nginx.conf
```

> **WICHTIG**: Das Nginx-Dockerfile baut die Config beim Build ein.
> Nach dem Kopieren muss `docker compose up -d --build` ausgefuehrt werden.

Die `docker-compose.yml` mountet `/etc/letsencrypt` direkt in den Container.
Die Zertifikate sind ohne Symlinks sofort verfuegbar.

## 7. Anwendung starten

```bash
cd /opt/ogn_monitor
docker compose up -d --build
```

Erster Build dauert ca. 2-5 Minuten (Python-Dependencies, npm install, Frontend-Build).

Pruefen ob alles laeuft:

```bash
docker compose ps
```

Erwartete Ausgabe - alle 5 Container "Up" / "healthy":

```
NAME                     STATUS
ogn_monitor-postgres-1   Up (healthy)
ogn_monitor-redis-1      Up (healthy)
ogn_monitor-worker-1     Up
ogn_monitor-api-1        Up (healthy)
ogn_monitor-nginx-1      Up (healthy)
```

Health-Check:

```bash
curl -s https://flight-monitor.de/health
# {"status":"ok","service":"api"}
```

## 8. Automatische Zertifikatserneuerung

Let's Encrypt Zertifikate sind 90 Tage gueltig.
Certbot erneuert automatisch per Systemd-Timer, aber Nginx muss
nach der Erneuerung die neuen Zertifikate laden.

### 8.1 Renewal-Hook einrichten

```bash
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'EOF'
#!/bin/bash
cd /opt/ogn_monitor
docker compose exec nginx nginx -s reload
EOF

chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

### 8.2 Renewal testen

```bash
certbot renew --dry-run
```

### 8.3 Certbot Timer pruefen

```bash
systemctl list-timers | grep certbot
```

Sollte `certbot.timer` zeigen, der 2x taeglich prueft.

## 9. Erste Schritte in der App

1. **Oeffne** https://flight-monitor.de
2. **Registrieren**: Flugplatz-Name, E-Mail, Passwort + Disclaimer akzeptieren
3. **Flugplatz konfigurieren**: Koordinaten, ICAO, Hoehe, Monitoring-Parameter
4. **Flugzeuge eintragen**: Kennzeichen + FLARM-IDs der Vereinsflugzeuge
5. **Tower-Monitor oeffnen**: Echtzeit-Anzeige unter `/monitor/<slug>`

## 10. Wartung & Monitoring

### Logs anschauen

```bash
cd /opt/ogn_monitor

# Alle Container
docker compose logs -f --tail 50

# Nur Worker (APRS-Verbindung)
docker compose logs -f worker

# Nur API
docker compose logs -f api

# Nur Fehler
docker compose logs api 2>&1 | grep -i error
```

### Update deployen

```bash
cd /opt/ogn_monitor
git pull origin master
docker compose up -d --build
```

### Datenbank-Backup

```bash
# Backup erstellen
docker compose exec postgres pg_dump -U ogn_monitor ogn_monitor \
  | gzip > /opt/ogn_monitor/backups/backup-$(date +%Y%m%d-%H%M).sql.gz

# Automatisches taegliches Backup per Cron
crontab -e
# Zeile hinzufuegen:
0 3 * * * cd /opt/ogn_monitor && docker compose exec -T postgres pg_dump -U ogn_monitor ogn_monitor | gzip > /opt/ogn_monitor/backups/backup-$(date +\%Y\%m\%d).sql.gz && find /opt/ogn_monitor/backups -name "*.gz" -mtime +30 -delete
```

### Neustart bei Problemen

```bash
docker compose restart        # Alle Container neustarten
docker compose restart worker # Nur Worker neustarten
docker compose down && docker compose up -d  # Komplett neu starten
```

## 11. Absicherung (empfohlen)

### SSH haerten

```bash
# /etc/ssh/sshd_config
PermitRootLogin no
PasswordAuthentication no
```

```bash
systemctl restart sshd
```

### Automatische Security-Updates

```bash
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

### Fail2ban (Brute-Force Schutz)

```bash
apt install -y fail2ban
systemctl enable fail2ban
```

---

## Zusammenfassung der Kosten

| Posten | Kosten/Monat |
|---|---|
| Hetzner CX22 (2 vCPU, 4 GB) | ~4.35 EUR |
| Domain flight-monitor.de | ~0.80 EUR (ca. 10 EUR/Jahr) |
| Let's Encrypt SSL | kostenlos |
| **Gesamt** | **~5.15 EUR/Monat** |

## Troubleshooting

**Container starten nicht?**
```bash
docker compose logs --tail 20
```

**502 Bad Gateway?**
API-Container ist noch nicht healthy. Warten oder pruefen:
```bash
docker compose logs api
```

**SSL-Fehler?**
Pruefen ob Zertifikate existieren:
```bash
ls -la /opt/ogn_monitor/nginx/ssl/
ls -la /etc/letsencrypt/live/flight-monitor.de/
```

**OGN-Verbindung bricht ab?**
Worker-Logs pruefen:
```bash
docker compose logs -f worker | grep -i "aprs\|connect\|error"
```

**DNS noch nicht propagiert?**
```bash
dig flight-monitor.de +short
# Muss die Server-IP zeigen
```
