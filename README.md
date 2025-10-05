# CCTV Alarm System

Sistem alarm CCTV yang memantau status kamera secara real-time dan memberikan notifikasi melalui telegram.

![GitHub Workflow Status](https://img.shields.io/github/workflow/status/username/repo/CI)
![Python Version](https://img.shields.io/badge/python-3.11-blue)

## Daftar Isi
- [Fitur Utama](#fitur-utama)
- [Arsitektur & Komponen](#arsitektur--komponen)
- [Prasyarat](#prasyarat)
- [Instalasi](#instalasi)
- [Konfigurasi](#konfigurasi)
- [How To Run?](#cara-menjalankan)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Deployment](#deployment)
- [Roadmap](#roadmap)

## Fitur Utama
- Memantau status kamera CCTV secara otomatis berupa mengirimkan PING ke setiap kamera.
- Mengirim notifikasi jika kamera mengalami masalah melalui telegram.
- Konfigurasi melalui file YAML untuk list ip camera.

## Arsitektur & Komponen
Sistem ini terdiri dari:
- **Kamera**: Terhubung ke jaringan dan dimonitoring realtime.
- **Backend**: Mengelola status kamera dan logika pemantauan.
- **Notifikasi**: Mengirimkan peringatan jika ada kamera yang tidak berfungsi.

## Requirements
- Python 3.11
- Package `openai` untuk interaksi dengan API OpenAI.

## Instalasi
1. Clone repository ini:
   ```bash
   git clone https://github.com/username/repo.git
   cd repo
   ```
2. Install dependensi:
   ```bash
   pip install -r requirements.txt
   ```

## Konfigurasi
Buat file `.env` di root direktori dan tambahkan variabel berikut:
```
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4o-mini
```

## Cara Menjalankan
Jalankan skrip utama:
```bash
python bot.py
```

## Testing
Testing dapat dilakukan dengan menjalankan command di telegram. Pastikan semua dependensi sudah terinstall.

## Troubleshooting
- Jika kamera tidak merespons, periksa koneksi jaringan.
- Pastikan konfigurasi di `cameras.yaml` sudah benar.

## Deployment
Untuk deployment,  dapat menggunakan Docker atau sistemd. Pastikan untuk mengonfigurasi file sesuai dengan ip segemntasi jaringan.

## Roadmap Fitur Terbaru
- [ ] Penambahan fitur analisis video.
- [ ] Integrasi dengan sistem notifikasi lebih lanjut.
- [ ] Peningkatan antarmuka pengguna untuk monitoring. 

Created by muciw - 2025
