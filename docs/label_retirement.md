# Penghapusan implementasi label WO dari Production App

## Follow-up aktivasi Product QR (2026-10-20)

Bagian penghapusan di bawah adalah riwayat. Setelah persetujuan user, tombol dan
dialog **milik Product QR** dipasang kembali di halaman WO yang sama. Production
App hanya menyimpan import/mount komponen serta pemicu tipis setelah simpan
packing; tidak mengembalikan API, QZ client atau penerbitan serial lokal.

Schema/controller mapping kini diekspor hanya oleh Product QR. Migrasi metadata
lokal in-place memindahkan module ke Product QR dan referensi serial kembali
menjadi Link ke Product QR Serial, tanpa mengganti nama tabel/record. Metadata
arsip lama di Production App sudah dihapus agar tidak ada dua exporter.

Saat deploy Cloud, sertakan **kedua app versi yang cocok**, termasuk source
`wo_labels_frontend` dan generated assets terbaru; install Product QR pada situs,
lalu migrate dan refresh/hard reload browser. Jangan deploy penghapusan lama
sendiri. Tidak ada deployment Cloud yang dijalankan oleh task ini.

Permintaan pengguna 2026-10-19: hapus hanya kode terkait label, tanpa perubahan
fitur produksi lain dan tanpa mengubah repository Product QR.

## Dihapus

- Tombol Cetak Label di WO, dialog konfirmasi/label tambahan, state dan handler
  khusus cetak. Dialog error umum untuk aksi produksi tetap ada.
- Tawaran cetak setelah simpan Pre-Packing/Post-Packing. Aksi simpan, payload,
  guard dan perhitungan packing tetap sama.
- API `work_order.get_label_data`, modul data label dan signing QZ, redirect
  `/qz-certificate`, dan halaman `/work_order_label`.
- Client QZ/ZPL, helper prompt/count/URL label, CSS tombol label dan tes fitur
  yang sudah dihapus.
- Dependency khusus label `qz-tray`, `pyqrcode`, `cryptography`, serta dependency
  app `product_qr`. Library Python pada environment bersama tidak di-uninstall.
- Salinan sertifikat/private key di folder **aplikasi**. Key di folder privat
  situs tidak disentuh; penghapusan source Git bukan pencabutan trust atau
  penghapusan key dari riwayat Git/clone.

## Arsip data tidak dihapus

DocType `Work Order QR Label` dipertahankan sebagai metadata arsip dengan
controller Document kosong, tanpa penerbitan/validasi serial atau logika cetak.
Field referensi serial menjadi Data, bukan Link ke app eksternal. Nama DocType,
nama field dan nilai historis tetap dipertahankan. Frappe v16 membersihkan
metadata DocType orphan saat migrate; menghapus controller/schema begitu saja
akan membuat arsip kehilangan definisi. Tidak dilakukan drop table, delete
record, migrasi ownership atau perubahan database dalam task ini.

Semua helper UOM, flag Tanpa Pre-Packing, field packing, langkah WO, operasi,
transfer bahan dan Manufacture tetap diperlukan proses produksi dan tidak
dihapus. Kata "label" pada label input/tabel biasa bukan fitur cetak label.

## Backup dan batas aktivasi

Backup source sebelum perubahan dan manifest hash tersedia di direktori privat
lokal `C:\Users\Ardhan\AppData\Local\Temp\opencode\production-label-removal-ncq4cq8j`.
Arsip ZIP tersebut **mengandung private key**: jangan unggah ke Git, public
files, chat atau tiket dukungan. Backup tidak dimasukkan ke repository.

Pada pemeriksaan task ini, Product QR sedang di branch main dan source hasil
copy sebelumnya tidak ada di working tree maupun tree branch wo-label yang
diperiksa. Tidak mengembalikan/mengubah branch atau file Product QR otomatis.

**Cetak dari Production App dinonaktifkan.** Ini bukan integrasi aktif ke Product
QR. Pulihkan/siapkan versi Product QR dan rencanakan ownership/mount/route label
terpisah sebelum mengaktifkan cetak lagi. Jangan deploy dengan asumsi tombol
lama otomatis berpindah ke Product QR.

## Verifikasi

- Frontend test dan build; tes retirement memeriksa hilangnya UI/pemicu/client
  serta tetap adanya dialog error umum dan aksi simpan packing.
- AST/backend structural test memeriksa endpoint/dependency dihapus sementara
  helper/aksi produksi dan metadata arsip tetap tersedia.
- Manifest hash sebelum/sesudah membatasi perubahan ke daftar file label,
  shared-file label hunks, tes/dokumentasi retirement dan generated build.
- Uji integrasi `test_wo_skip_prepacking` dicoba tetapi keempat kasus terhenti
  pada fixture: `Could not find Item Group: Produk Jadi Pabrik`. Tidak membuat
  master data atau mengubah fixture di luar scope untuk memaksakan kelulusan.
- Belum browser interaktif/deploy Cloud; tidak commit/push/full migrate.
