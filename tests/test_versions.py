"""Bir xil xabarni aniqlash (versions.same_story): haqiqiy topilmalardan olingan holatlar.
python -m pytest tests"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor import versions as ver  # noqa: E402


def rec(url, title="", date="2026-11-25T12:00:00+05:00", nums=(), img="", links=(), outlet=None, lang=None):
    r = {"url": url, "title": title, "date": date, "nums": list(nums), "img": img, "links": list(links),
         "lang": lang or ver.lang_label(url, title)}
    r["outlet"] = outlet or ver.site_key(url)
    return r


# --- birlashtirilmasligi kerak (boshqa-boshqa xabarlar) ---

def test_tg_footer_self_link_not_same():
    # har postning tagida kanalning o'z havolasi bor — bu umumiy maqola havolasi emas
    a = rec("https://t.me/Rasmiy_xabarlar_Official/60500", "Ўзбекистонда аҳолининг ўртача ёши 29,6 ёш",
            links=["https://t.me/Rasmiy_xabarlar_Official"], nums=["29.6"])
    b = rec("https://t.me/Rasmiy_xabarlar_Official/60093", "Октябрь ойида йиллик инфляция 6,5 фоизни ташкил этди",
            links=["https://t.me/Rasmiy_xabarlar_Official"], nums=["6.5"])
    assert not ver.same_story(a, b)


def test_tg_shared_invite_link_not_same_outlet():
    # ikki xil kanal tagida bitta taklif havolasi — bitta nashr emas
    a = rec("https://t.me/Qashqadaryo_Xalqi_qarshiliklar_7/128645", "🥩",
            links=["https://t.me/+UjOf6LoeHTEyZWQy"], outlet="t.me/+ujof6loehteyzwqy")
    b = rec("https://t.me/Qashqadaryo_shahrisabz_xalqi/84290", "🥩",
            links=["https://t.me/+UjOf6LoeHTEyZWQy"], outlet="t.me/+ujof6loehteyzwqy")
    assert ver.outlet(a) == "t.me/qashqadaryo_xalqi_qarshiliklar_7"
    assert not ver.same_story(a, b)


def test_site_footer_link_not_same():
    a = rec("https://t.me/uzboomtv/53607", "‼️", links=["https://uzboomtv.com/download-app"], outlet="uzboomtv.com")
    b = rec("https://t.me/uzboomtv/53756", "‼️", links=["https://uzboomtv.com/download-app"], outlet="uzboomtv.com")
    assert not ver.same_story(a, b)


def test_generic_image_not_same():
    a = rec("https://anhor.uz/ru/news/obrazovanie-uzbekistane-34-8-trln-sumov",
            "Услуги в сфере образования Узбекистана: 34,8 трлн сумов", img="social", nums=["34.8", "19.6"])
    b = rec("https://anhor.uz/uz/iqtisodiyot/ozbekistonda-tog-kon-sanoati-hajmi-70-trln-somdan-oshdi",
            "Ўзбекистонда тоғ-кон саноати 70 трлн сўмдан ошди", img="social", nums=["70"])
    assert not ver.same_story(a, b)
    assert ver.image_key("https://anhor.uz/img/social.jpg") == ""


def test_stock_image_different_title_numbers():
    # uzdaily bitta stok rasmni turli xabarlarga qo'yadi
    a = rec("https://www.uzdaily.uz/ru/obem-obrazovatelnykh-uslug-v-uzbekistane-vyros-na-196/",
            "Объем образовательных услуг в Узбекистане вырос на 19,6%", img="iutstudents_20260613094949",
            nums=["19.6", "34.8", "18.6"])
    b = rec("https://www.uzdaily.uz/ru/v-uzbekistane-obrazovaniem-okhvacheny-96-mln-chelovek-starshe-7-let/",
            "В Узбекистане образованием охвачены 9,6 млн человек старше 7 лет", img="iutstudents_20260613094949",
            nums=["9.6", "19.6"])
    assert not ver.same_story(a, b)


def test_year_is_not_article_id():
    a = rec("https://infoedu.uz/talim-xizmatlari-hajmi-34-8-trln-som-2026", "Ta’lim xizmatlari hajmi 34,8 trln")
    b = rec("https://infoedu.uz/oliy-malumotli-aholi-12-foiz-statistika-2026", "Oliy ma’lumotli aholi 12 foiz")
    assert not ver.same_story(a, b)


def test_generic_page_title_different_ids():
    a = rec("https://rasmiy24.uz/uz/article/1198/", "Rasmiy24 - Latest News and Articles")
    b = rec("https://rasmiy24.uz/uz/article/1185/", "Rasmiy24 - Latest News and Articles")
    assert not ver.same_story(a, b)


def test_campaign_dates_are_not_common_numbers():
    # biznesni ro'yxatga olish postlari: "15-oktabrdan 30-noyabrgacha" — har xil postlarda bir xil
    a = rec("https://t.me/the_bakiroo/13772", "Бизнесни рўйхатга олишда иштирок этиш қанчалик муҳим?",
            nums=["15", "30", "1851"])
    b = rec("https://t.me/the_bakiroo/13598", "Бизнесни рўйхатга олиш нима учун керак?", nums=["15", "30", "1851"])
    assert not ver.same_story(a, b)


# --- birlashtirilishi kerak (bitta xabar) ---

def test_language_path_same():
    a = rec("https://www.gazeta.uz/oz/2026/11/24/sugar-uzbekistan/", "Taqchillik va narxlarning keskin oshishi")
    b = rec("https://www.gazeta.uz/ru/2026/11/24/sugar-uzbekistan/", "Прямой путь к дефициту и скачку цен")
    assert ver.same_story(a, b)


def test_same_image_translation():
    a = rec("https://zamon.uz/ru/2026/11/24/import-govyadiny-v-uzbekistan-vyros-na-177",
            "Импорт говядины в Узбекистан вырос на 17,7%", img="20251025_1354431-1_5k4rec6.og",
            nums=["17.7", "55.9", "597.7"])
    b = rec("https://zamon.uz/uz/2026/11/24/ozbekistonda-mol-goshti-importi-177-foizga-oshdi",
            "O'zbekistonda mol go‘shti importi 17,7 foizga oshdi", img="20251025_1354431-1_5k4rec6.og",
            nums=["17.7", "55.9", "597.7"])
    assert ver.same_story(a, b)


def test_same_image_rounded_number():
    a = rec("https://www.uzdaily.uz/en/uzbekistan-has-357883-active-individual-entrepreneurs/",
            "Uzbekistan Has 357,883 Active Individual Entrepreneurs", img="hunarmand2_20260925035009")
    b = rec("https://www.uzdaily.uz/ru/v-uzbekistane-naschityvaetsia-3579-tys-individualnykh-predprinimatele",
            "В Узбекистане насчитывается 357,9 тыс. индивидуальных предпринимателей", img="hunarmand2_20260925035009")
    assert ver.same_story(a, b)


def test_tg_post_links_own_site_article():
    url = "https://podrobno.uz/cat/obchestvo/uzbekistan-stanovitsya-starshe-sredniy-vozrast-naseleniya-priblizilsya-k-30"
    a = rec("https://t.me/podrobno/93558", "▫️", links=[url], outlet="podrobno.uz")
    b = rec(url, "Узбекистан становится старше — средний возраст населения приблизился к 30 годам")
    assert ver.same_story(a, b)


def test_tg_repost_same_numbers():
    nums = ["12", "132", "17.7", "24", "55.9", "597.7"]
    a = rec("https://t.me/ovozli24/20482", "#Import", nums=nums)
    b = rec("https://t.me/ovozli24/20461", "#Import", nums=nums)
    assert ver.same_story(a, b)
