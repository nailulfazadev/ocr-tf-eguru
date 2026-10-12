# Cek Bukti TF e-guru.id (aplikasi desktop tim)

Membaca gambar bukti transfer dari link Drive di Google Sheets, memvalidasinya, lalu mencentang
kolom ceklis dan mengisi catatan. Windows & Mac. Tesseract (lokal, tanpa kuota) membaca gambar;
Gemini hanya dipanggil untuk bukti yang meragukan.

## Cara kerja singkat

1. Baca baris sheet. **Baris dilewati** bila ceklis sudah tercentang atau kolom catatan sudah terisi.
2. Klaim blok baris (tulis penanda `[diproses|nama|waktu]` di catatan) supaya anggota tim lain tidak
   mengerjakan baris yang sama. Klaim yang macet > 30 menit dianggap kedaluwarsa.
3. Unduh gambar (disimpan di cache) -> pra-proses -> Tesseract -> regex ambil nominal/tanggal/referensi/status.
4. Validasi dengan **aturan pasti** (nominal = kolom nominal, rekening/nama tujuan = kolom bank, tanggal bukti tidak
   sebelum *tanggal minimal* (opsional, di Pengaturan) dan wajar terhadap waktu isi form (7 hari sebelum s/d 1 hari sesudah),
   status berhasil, nomor referensi tidak duplikat).
5. Hasil OCR **tidak pernah langsung menolak** karena nominal beda (bisa salah baca): dicek ulang oleh Gemini.
   Penolakan final hanya dari: nominal beda atau tanggal sebelum batas minimal yang dikonfirmasi Gemini, atau nomor referensi duplikat.
6. Tulis ke sheet (batch): ceklis, catatan (`VALID | Rp20.000 | 15/09/2026 21:30 | ref ... | ocr/Budi 10/10 14:02`),
   dan nomor referensi. Sebelum menulis, catatan dicek ulang; bila diubah orang lain, hasil tidak ditimpa.
7. Yang berstatus `CEK MANUAL` dikerjakan di tab **Review manual** (tombol V = valid, T = tolak, S = lewati).

## Pilihan login: service account (disarankan untuk tim) atau OAuth

**A. Service account** - tanpa login, tanpa batas 7 hari. Letakkan `service_account.json` di `~/.bukti_tf/`
(Windows: `C:\Users\NAMA\.bukti_tf\`). Aplikasi otomatis memakainya bila file ada.
1. Google Cloud: aktifkan *Google Sheets API* dan *Google Drive API*, buat service account, buat kunci JSON.
2. **Bagikan spreadsheet** ke email service account (`...@...iam.gserviceaccount.com`) sebagai **Editor**.
3. **Bagikan folder upload Google Form** (folder di Drive tempat bukti tersimpan) ke email yang sama sebagai **Viewer**.
4. Kunci JSON adalah rahasia: jangan kirim lewat chat, jangan masuk GitHub. Satu file yang sama dipakai tiap komputer.
   Jika bocor, hapus kuncinya di Google Cloud lalu buat yang baru.
Catatan: semua perubahan di sheet tercatat atas nama service account; nama petugas tetap tertulis di kolom catatan.

**B. OAuth** (login Google per orang) - lihat setup di bawah. Dipakai bila `service_account.json` tidak ada.

## Setup OAuth (hanya bila tidak memakai service account)

1. **Google Cloud Console** -> buat project -> aktifkan *Google Sheets API* dan *Google Drive API*.
2. *OAuth consent screen*: tipe **Internal** bila memakai Google Workspace; bila tidak, tipe External mode *Testing*
   lalu tambahkan email semua anggota tim sebagai *Test users*. (Mode Testing: login perlu diulang tiap ~7 hari.)
3. *Credentials* -> **Create OAuth client ID** -> tipe **Desktop app** -> unduh JSON, ganti nama jadi
   `client_secret.json`. Bagikan file ini ke tim; tiap anggota menaruhnya di folder aplikasi atau di
   `~/.bukti_tf/client_secret.json`.
4. Semua anggota harus punya akses **edit** ke spreadsheet dan **lihat** ke file upload form di Drive.
5. API key Gemini gratis: <https://aistudio.google.com/apikey>. Model default `gemini-3.5-flash-lite`. Google sering
   mengganti/mempensiunkan model; di Pengaturan klik **Tes Gemini** untuk menguji key + model dan melihat daftar model
   yang tersedia untuk key Anda. Model lama (2.5 ke bawah) otomatis dipindah ke default.

## Tanpa terminal (klik saja)

**Mac / Windows, dari kode:** setelah pemasangan pertama, klik dua kali `jalankan.command` (Mac) atau `jalankan.bat` (Windows).

**Aplikasi mandiri (.app / .exe):** klik dua kali `build_mac.command` (di Mac) atau `build_windows.bat` (di Windows).
Hasilnya: `dist/Cek Bukti TF e-guru.id.app` (Mac) atau `dist/Cek Bukti TF e-guru.id/Cek Bukti TF e-guru.id.exe` (Windows). Bagikan hasilnya ke tim
(zip foldernya). Build harus dilakukan di sistem yang sama dengan target, dan di Mac harus sesuai chip
(Apple Silicon atau Intel). Penerima tidak perlu memasang Python.
- Mac: bila diblokir saat pertama dibuka, klik kanan -> *Open* -> *Open*.
- Windows: bila muncul "Windows protected your PC", klik *More info* -> *Run anyway*.

**Pengaturan pertama, semua lewat aplikasi:** tab Pengaturan -> *Pilih file kunci (.json)* (file service account atau
client_secret; aplikasi menyalin dan menamainya otomatis) -> isi URL sheet -> *Hubungkan* -> isi API key Gemini -> Simpan.

## Menjalankan dari kode

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Tesseract (+ data bahasa Indonesia):
#   Mac:     brew install tesseract tesseract-lang
#   Windows: installer UB Mannheim, centang bahasa Indonesian
python main.py
```
Di tab **Pengaturan**: isi nama, tempel URL sheet, klik *Hubungkan*, atur kolom (default: link=H, nominal=G,
rekening=F, ceklis=I, catatan=K, referensi=L), isi API key Gemini, simpan. Lalu tab **Proses** -> *Mulai Cek*.

> Kolom catatan dan referensi harus kolom kosong. Di sheet Anda, J1 berisi angka, jadi catatan dipasang di K.

## Tes

```bash
python -m pytest tests -q        # logika, pipeline (OCR sungguhan, sheet palsu), tombol review
```

## Membuat aplikasi (.exe / .app)

PyInstaller tidak bisa lintas sistem, jadi build dilakukan per sistem (otomatis lewat GitHub Actions,
lihat `.github/workflows/build.yml`) atau manual:

```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --name "Cek Bukti TF e-guru.id" --collect-data googleapiclient --collect-data certifi main.py
```
- **Windows**: salin folder `Tesseract-OCR` ke `vendor/tesseract/` (berisi `tesseract.exe` dan `tessdata/` dengan
  `eng` + `ind`) sebelum build, agar tidak perlu instal terpisah.
- **Mac**: membundel Tesseract bersama dylib-nya rumit; cara termudah: tiap Mac menjalankan
  `brew install tesseract tesseract-lang` sekali. Aplikasi mencari di `/opt/homebrew/bin` dan `/usr/local/bin`.
  Tanpa tanda tangan Apple Developer, buka pertama kali dengan klik kanan -> *Open*.

## Banyak event sekaligus

Satu aplikasi, banyak event. Di kiri ada daftar **Event** (tombol: Event baru, Duplikat, Ganti nama, Hapus). Tiap event
punya spreadsheet, kolom, donasi minimal, tanggal minimal, dan rentang baris sendiri, dan **bisa dijalankan bersamaan**
(tanda ▶ muncul di event yang sedang berjalan). **Pengaturan umum** (tombol di kiri bawah) berlaku untuk semua event:
kunci Google, nama petugas, API key Gemini, dan kecepatan. Semua event memakai **satu pembaca Gemini bersama**, jadi
laju request dibagi rata dan bila kuota habis semua event ikut berhenti memanggil Gemini (tidak membuang request).
Mau tim berbeda mengerjakan event berbeda di laptop berbeda: tiap laptop cukup membuka event yang sama; sistem klaim
baris mencegah baris dikerjakan dua kali.

## Gemini berbayar (lebih cepat)

Di Pengaturan umum pilih **Mode akun: Berbayar** (billing proyek Google Cloud harus aktif). Otomatis memakai 120 request/menit
dan 6 panggilan bersamaan; isi manual sesuai batas akun Anda (AI Studio > Rate limits). Bila terkena batas per menit
(429), semua pekerjaan menunggu bersama lalu mencoba lagi (maks 4 kali); batas harian langsung menghentikan proses.
Mode gratis tetap pelan dan hemat kuota (13 request/menit, 1 panggilan).

## Toleransi nominal, bukti kembar, dan melanjutkan proses

- **Toleransi selisih nominal** (Pengaturan event; default **± Rp6.500**, isi 0 = harus persis): nominal di bukti boleh lebih
  atau kurang dari sheet sampai batas itu (mis. biaya admin bank). Contoh: sheet 50.000, bukti 52.500 -> valid, dengan
  catatan "selisih +Rp2.500 dalam toleransi Rp6.500". Donasi minimal dihitung dari nilai yang lebih kecil antara sheet dan bukti.
  Selisih di atas toleransi tetap diperiksa seperti biasa (CEK MANUAL / TIDAK VALID).
- **Bukti kembar** = nomor referensi **dan** tanggal **dan** jam (sampai detik) sama. Baris kedua menjadi TIDAK VALID, dan
  sel **bukti (kolom link)** kedua baris diberi warna **kuning** di spreadsheet. Referensi yang sama tetapi jam berbeda
  bukan kembar. Kolom referensi kini berisi `REFERENSI|tanggalTjam` (nilai lama tanpa tanggal tetap dikenali).
- **Berhenti / Jeda / lanjut:** Jeda menahan proses, klik Lanjut untuk meneruskan. Berhenti atau menutup aplikasi aman:
  hasil yang selesai sudah di sheet. **Mulai / Lanjutkan Cek** melewati baris yang sudah ada hasilnya dan memproses sisanya.
  Klaim sisa proses yang macet di komputer yang sama langsung diambil kembali (komputer lain menunggu 30 menit).
- **Mengulang dengan aturan baru** (Pengaturan event > "Baris yang sudah ada hasilnya"): *Lanjutkan saja* (default) |
  *Ulangi juga baris CEK MANUAL* | *Ulangi SEMUA hasil otomatis*. Keputusan manual (`VALID (manual)` / `TIDAK VALID (manual)`)
  dan ceklis tanpa catatan tidak pernah diulang.

## Aturan tanggal

- **Tanggal minimal** (Pengaturan) bersifat **inklusif**: bukti bertanggal sama dengan atau setelah tanggal itu diterima.
  Bila diisi, ini menjadi batas bawah satu-satunya (aturan "7 hari sebelum waktu isi form" tidak dipakai lagi).
- Selalu berlaku: bukti tidak boleh bertanggal lebih dari 1 hari **setelah** form diisi, dan tidak boleh di masa depan.
- Gemini diberi tahu tanggal hari ini dan tidak menilai kewajaran tanggal; kewajaran tanggal diputuskan aturan di atas.
- **Proses ulang baris CEK MANUAL** (Pengaturan): memproses ulang baris yang statusnya CEK MANUAL otomatis (mis. setelah
  aturan diperbarui). Keputusan manual ("VALID (manual)" / "TIDAK VALID (manual)") tidak pernah diubah.

## Donasi minimal & tab Review

- **Donasi minimal** (Pengaturan): bukti yang nominalnya sudah cocok dengan sheet tetapi kurang dari minimal otomatis
  **TIDAK VALID** (batas inklusif: minimal 10.000 menerima 10.000). 0 = tanpa minimal.
- **Tab Review** memutuskan baris `CEK MANUAL` (yang aplikasi ragu) dan bisa memeriksa ulang hasil otomatis lewat filter
  (Tidak valid / Valid / Semua). Keputusan manual menimpa hasil otomatis dan tercatat dengan nama petugas.

## Tanpa Tesseract (mode Gemini saja)

Bila Tesseract tidak bisa dipasang (mis. Homebrew gagal di macOS lama), isi API key Gemini dan centang
*Pakai Gemini*: aplikasi otomatis beralih ke mode **Gemini saja** (semua bukti dibaca Gemini). Lebih akurat, tetapi
memakai kuota harian gratis; bila habis, proses berhenti dan baris yang belum selesai tetap bisa dilanjutkan besok.

## Batasan

- Memeriksa **isi dan konsistensi**, bukan keaslian gambar. Gemini diminta menandai kejanggalan, tapi bukan jaminan.
  Cocokkan juga dengan mutasi rekening.
- Hanya file gambar. PDF dan file non-gambar masuk `CEK MANUAL`. Bila ada beberapa file per baris, hanya yang pertama dicek.
- Deteksi duplikat memakai nomor referensi; bukti tanpa nomor referensi terbaca tidak bisa dicek duplikatnya.
- Kunci API Gemini disimpan polos di `~/.bukti_tf/config.json` (atau pakai variabel lingkungan `GEMINI_API_KEY`).
- Cache gambar di `~/.bukti_tf/cache` tidak dibersihkan otomatis.
