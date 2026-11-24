# OAV monitoring — Milliy statistika qo'mitasi

Bu tizim matbuotda qo'mita haqida chiqqan materiallarni avtomatik topadi va har birini t.me/statistikaoav arxiv kanaliga alohida post qilib tashlaydi. Kanaldan «OAV monitoring jadvali» (Apps Script) ularni o'zi oladi. Tekshirish kerak bo'lgan topilmalar, xatolar va kunlik yakun bot orqali egasiga keladi.

## Qanday ishlaydi

Tizim GitHub Actions'da o'zi ishga tushadi. Kompyuterni yoqib turish shart emas.

Kunduzi (07:00–24:00) **har 30 daqiqada**, kechasi **soatda bir marta**. Tashqi budilnik (Google Apps Script) har 30 daqiqada ishga tushiradi; ortiqcha chaqiruvlarni tizim o'zi o'tkazib yuboradi.

Kechasi (23:00–07:00) xabarlar **ovozsiz** keladi. Har kuni soat 21:00 dan keyin **kunlik yakun** keladi: nechta chiqish topildi va qaysi manbalarda. Bu xabar tizim ishlab turganini ham bildiradi.

Har bir ishga tushishda manbalar uch qatlamda tekshiriladi:

1. **Qidiruv tizimlari (Google News, Bing News).** 30 dan ortiq so'rov 16 tilda beriladi: o'zbek (lotin, kirill), rus, ingliz, turk, ozarbayjon, qozoq, qirg'iz, tojik, fransuz, nemis, ispan, arab, fors, xitoy, koreys, yapon. Bu qatlam xorijiy OAVni qamrab oladi.
2. **Saytlarning RSS lentalari.** Tizim ularni o'zi topadi.
3. **Saytning o'zi.** Bosh sahifa va bo'limlar ochiladi, RSS bo'lmasa sitemap ko'riladi. Har bir yangi maqolaning **to'liq matni** o'qiladi, chunki qo'mita nomi ko'pincha sarlavhada emas, matn ichida bo'ladi.

4. **Telegram kanallari.** stat.uz «OAV biz haqimizda» hisobotlari va t.me/statistikaoav kanalida uchragan 900 dan ortiq ochiq kanal t.me/s/ sahifasi orqali kuzatiladi (login shart emas). Oxirgi 36 soatdagi postlar tekshiriladi.

Bir marta yuborilgan maqola qayta yuborilmaydi. Bitta saytdagi maqolaning boshqa tildagi versiyasi kanalga qayta tashlanmaydi. Ikkalasi bitta tekshiruvda topilsa, o'zbekchasi yuboriladi (lotin, keyin kirill, keyin rus). OAVning rasmiy Telegram kanali o'z saytidagi maqolaga havola bersa, kanalga sayt havolasi ketadi. Barcha topilmalar `data/found.jsonl` faylida saqlanadi.

Google News havolalari (news.google.com/...) asl manzilga aylantiriladi va maqolaning o'zi tekshiriladi. Aylantirib bo'lmasa, topilma kanalga emas, egasiga «tekshiring» belgisi bilan keladi.

## Kanal posti

Sayt:
```
https://kun.uz/news/2026/11/24/...
📰 Sarlavha
🗓 24.11.2026
┃ …matndan parcha, topilgan so'z qalin…
```

Telegram kanal:
```
https://t.me/militsya_uzbek/88840
24.11 18:42
```

Postda faqat bitta havola bo'ladi (jadval har bir havolani alohida qator qiladi). Kanal postlari ovozsiz ketadi va keyin tahrirlanmaydi.

## Sozlash (kod yozmasdan)

Hamma sozlamalar `config/` papkasida. GitHub saytida faylni ochib, ✏️ (Edit) tugmasini bosing, o'zgartiring va **Commit changes** ni bosing. Keyingi ishga tushishda o'zgarish kuchga kiradi.

- `config/keywords.yml` — qo'mita nomlari (barcha tillarda).
  - Tutuq belgisini (') yozmang: `statistika qomita`.
  - So'z o'zagini yozish yetarli: «qo'mitasi», «qo'mitasining» kabi qo'shimchali shakllarni o'zi topadi.
  - Umumiy iboralar («statistika ma'lumotlariga ko'ra») hozir o'chiq: `weak: enabled: false`. Yoqsangiz, chiqishlar ko'payadi, lekin ular orasida keraksizlari ham ko'p bo'ladi.
- `config/sources.yml` — saytlar ro'yxati va qidiruv so'rovlari. Yangi sayt qo'shish uchun bitta qator kifoya:
  ```
  - {domain: yangisayt.uz, pages: ['/']}
  ```

## Qo'lda ishga tushirish

**Actions** → **OAV monitoring** → **Run workflow**.

## Aniqlik qoidalari

- **«Milliy statistika qo'mitasi», «Нацкомстат» kabi aniq nomlar** har doim hisoblanadi. Faqat bitta istisno bor: yonida boshqa davlat nomi tursa, masalan «Tojikiston statistika agentligi», bu boshqa davlat idorasi deb hisoblanadi.
- **«Statistics committee», «комитет по статистике» kabi umumiy nomlar** faqat yonida yoki sarlavhada O'zbekiston nomi bo'lsa hisoblanadi.
- **qo'mitaning o'z saytlari** (stat.uz, siat.stat.uz) monitoringga kirmaydi.

## Cheklovlar

- **Xorijdan ochilmaydigan saytlar.** Ba'zi O'zbekiston saytlari GitHub serverlaridan ochilmaydi, masalan xabar.uz, bugun.uz, jahonnews.uz. Bunday saytlardagi chiqishlar Google News orqali ushlanadi. Ularni ham to'g'ridan-to'g'ri tekshirish uchun tizimning bir qismini O'zbekistondagi kompyuterda ishlatish kerak bo'ladi (GitHub «self-hosted runner»).
- **Repo ochiq (public).** GitHub daqiqalari cheklanmaydi. Bot tokeni va chat ID «Secrets»da yashirin turadi, ular hech kimga ko'rinmaydi.
- **Yopiq Telegram kanallari** va guruhlar kuzatilmaydi (faqat ochiq kanallar).
