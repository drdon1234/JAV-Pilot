<p align="center">
  <img src="../images/banner_ar.svg" alt="JAV Pilot" width="100%">
</p>

<div align="center">

<a href="../../README.md">简体中文</a> |
<a href="README_zh-TW.md">繁體中文</a> |
<a href="README_en.md">English</a> |
<a href="README_ja.md">日本語</a> |
<a href="README_fr.md">Français</a> |
<a href="README_es.md">Español</a> |
<a href="README_ru.md">Русский</a> |
<b>العربية</b>

<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/v/drdon1234/jav-pilot?sort=semver&label=docker&color=3567b7" alt="إصدار Docker"></a>
<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/pulls/drdon1234/jav-pilot?color=3567b7" alt="مرات السحب من Docker"></a>
<img src="https://img.shields.io/badge/platform-linux%2Famd64-3567b7" alt="المنصة">
<img src="https://img.shields.io/badge/python-3.11+-3567b7" alt="Python 3.11+">
<a href="../../LICENSE"><img src="https://img.shields.io/badge/license-MIT-3567b7" alt="ترخيص MIT"></a>
<br>

<a href="../guide/getting-started.md">التثبيت</a> |
<a href="../guide/usage.md">الاستخدام</a> |
<a href="../guide/faq.md">الأسئلة الشائعة</a> |
<a href="../guide/configuration.md">الإعدادات</a> |
<a href="https://github.com/drdon1234/JAV-Pilot/issues">الإبلاغ عن مشكلة</a>

</div>

<br>

‏JAV Pilot تطبيق ذاتي الاستضافة للبحث عن مقاطع الفيديو وتنزيلها وإدارة مكتبة الوسائط، ويعمل على جهاز NAS أو خادم Linux أو حاسوب شخصي. يجمع التطبيق نتائج البحث من عدة مواقع للبيانات الوصفية، وينزّل عبر qBittorrent أو مباشرةً من مواقع الفيديو، ثم ينقل كل تنزيل مكتمل إلى مكتبة الوسائط مع الملصقات وبيانات NFO الوصفية التي يتعرّف عليها Jellyfin وEmby وKodi مباشرةً. تبقى جميع الإعدادات والسجلات وملفات الوسائط على جهازك، وتعمل واجهة الويب في متصفحات أجهزة الحاسوب والأجهزة المحمولة.

> [!NOTE]
> الوثائق التفصيلية في المجلد `docs/`‎ متوفرة حاليًا باللغة الصينية المبسطة فقط. أما واجهة الويب فمتوفرة بالعربية وتتبع لغة المتصفح والنظام افتراضيًا.

<p align="center">
  <img src="../images/search.png" alt="نتائج البحث في JAV Pilot" width="92%">
  <br>
  <sub>تستخدم جميع لقطات الشاشة بيانات نموذجية وهمية.</sub>
</p>

## ✨ الميزات

1. 🔍 **البحث في عدة مواقع**: يستعلم بالتوازي من JavBus وJavDB وFC2 وغيرها من مواقع البيانات الوصفية، ويدمج نتائج العمل الواحد حسب رمز الكتالوج (番号)، ويحتفظ بمصدر كل حقل.
2. 🧲 **تجميع روابط magnet**: يعرض حجم الملف ومصدره ومعلومات الترجمة، ويزيل التكرار حسب info hash؛ ويمكن إضافة Sukebei وTokyo Toshokan عبر Jackett.
3. ⬇️ **قناتا تنزيل**: تُرسَل روابط magnet إلى qBittorrent، أما تنزيلات الويب فتختار الجودة تلقائيًا وتفضّل النسخة الأصلية ثم المترجمة إلى الصينية ثم غير الخاضعة للرقابة بهذا الترتيب.
4. 🔁 **كشف التكرار**: قبل إنشاء أي مهمة، تُطابَق مهام qBittorrent ومهام تنزيل الويب ومكتبة الوسائط لتجنّب تنزيل العمل نفسه مرتين.
5. 🗂️ **التنظيم التلقائي**: تُؤرشَف التنزيلات المكتملة حسب رمز الكتالوج، وتُنشأ الملصقات وصور الخلفية وبيانات NFO الوصفية.
6. 📚 **إدارة مكتبة الوسائط**: يفحص مجلد الفيديو، بما في ذلك الملفات المضافة يدويًا، ويستكمل الملصقات والبيانات الوصفية الناقصة.
7. 🏆 **التصنيفات**: تصنيفات الأعمال والممثلات والفئات من JavDB وFANZA وFC2 وMGStage وغيرها، مع جلب التفاصيل دفعةً واحدة في الخلفية.
8. 🌐 **ترجمة العناوين**: خدمة ترجمة عامة مدمجة، مع دعم خدمات الذكاء الاصطناعي مثل الواجهات المتوافقة مع OpenAI وClaude وGemini وOllama؛ وتُترجم العناوين افتراضيًا إلى لغة الواجهة.
9. 🩺 **تشخيص المواقع**: يتحقق دوريًا من توفّر المواقع ويميّز بين إخفاقات DNS والاتصال وTLS والتحقق من البشر وتغيّر بنية الصفحات.
10. 🔔 **الإشعارات**: يرسل إشعارات عبر Webhook أو Gotify أو Telegram أو إشعارات NAS عند اكتمال التنزيل أو فشله، أو انخفاض مساحة القرص، أو تعطّل أحد المواقع.
11. 📱 **واجهة متجاوبة متعددة اللغات**: متوفرة بثماني لغات (العربية والإنجليزية والصينية المبسطة والتقليدية واليابانية والفرنسية والإسبانية والروسية) وتتبع لغة المتصفح والنظام افتراضيًا؛ تعمل على أجهزة الحاسوب والأجهزة المحمولة، مع سمتين فاتحة وداكنة.

## 🚀 البدء السريع

تتوفر ثلاث طرق للتثبيت، ويُوصى بالطريقة الأولى عند النشر لأول مرة.

<div dir="rtl">

| الطريقة | حالة الاستخدام | المتطلبات المسبقة |
| --- | --- | --- |
| [الطريقة 1: Docker (موصى بها)](#الطريقة-1-النشر-باستخدام-docker-موصى-بها) | جهاز NAS أو خادم Linux أو WSL2 على Windows | Docker وDocker Compose v2 |
| [الطريقة 2: البناء من الشيفرة المصدرية](#الطريقة-2-بناء-الصورة-من-الشيفرة-المصدرية) | تعديل الشيفرة المصدرية أو بناء صورة خاصة | Docker وGit |
| [الطريقة 3: دون Docker](#الطريقة-3-دون-docker) | عدم توفّر Docker، أو التشغيل كخدمة نظام | Git وPython 3.11+ وNode.js 24 |

</div>

### الطريقة 1: النشر باستخدام Docker (موصى بها)

تستخدم هذه الطريقة الصورة المنشورة على Docker Hub، ولا تحتاج إلا إلى ملف `docker-compose.yml` وملف ‎`.env`.

> [!NOTE]
> تدعم الصورة معمارية x86_64 (amd64) فقط. على Windows، نفّذ الخطوات التالية داخل WSL2.

**1. اختر ملف Compose**

ينشر كل ملف Compose الخدمات التالية:

<div dir="rtl">

| ملف Compose | الخدمات المنشورة |
| --- | --- |
| `docker-compose.yml` | JAV Pilot |
| `deploy/docker-compose.jackett.yml` | JAV Pilot، Jackett |
| `deploy/docker-compose.qbittorrent.yml` | JAV Pilot، qBittorrent |
| `deploy/docker-compose.full.yml` | JAV Pilot، qBittorrent، Jackett |

</div>

‏qBittorrent عميل BitTorrent، وJackett يوفّر واجهة Torznab لـ Sukebei وTokyo Toshokan. تحمل الحاويات التي تُنشر مع JAV Pilot الاسمين `jav-pilot-qbittorrent` و`jav-pilot-jackett`، فلا تتعارض مع الحاويات الموجودة من النوع نفسه.

**2. نزّل ملف Compose وقالب الإعدادات**

```bash
mkdir jav-pilot && cd jav-pilot
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/.env.example
```

إذا اخترت في الخطوة 1 ملفًا من المجلد `deploy/`‎، فاستبدل `main/docker-compose.yml` في الأمر الثاني بمساره (مثل `main/deploy/docker-compose.full.yml`)، مع الإبقاء على اسم الملف المحفوظ `docker-compose.yml`.

**3. املأ الملف ‎`.env`**

نفّذ الأمر التالي لإنشاء مفتاح الجلسة وتعيين هوية التشغيل (`PUID` / `PGID`) إلى الحساب الحالي:

```bash
sed -i "s/^JAV_PILOT_AUTH_SECRET=.*/JAV_PILOT_AUTH_SECRET=$(openssl rand -hex 32)/; s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

ثم حرّر الملف ‎`.env` (مثلًا باستخدام `nano .env`):

- ‏`JAV_PILOT_AUTH_PASSWORD`: كلمة مرور تسجيل الدخول، 12 حرفًا على الأقل، وهي إلزامية.
- ‏`QBITTORRENT_PASSWORD`: تُعبّأ عند نشر qBittorrent مع JAV Pilot، 6 أحرف على الأقل. تصبح كلمة مرور واجهة الويب في qBittorrent (اسم المستخدم `admin`)، ويتصل JAV Pilot تلقائيًا ببيانات الاعتماد نفسها.
- المجلدات: توجد مكتبة الوسائط افتراضيًا في `media/`‎ داخل المجلد الحالي. لاستخدام مجلد آخر على جهاز NAS، عدّل قسم «目录» (المجلدات) في الملف ‎`.env`.

**4. شغّل الخدمة**

```bash
docker compose up -d
```

يسحب التشغيل الأول صورة يبلغ حجمها نحو 1.1 غيغابايت. بعد ذلك افتح `http://<host-ip>:8766` في المتصفح (حيث ‎`<host-ip>`‎ هو عنوان IP للمضيف)، وسجّل الدخول باسم المستخدم `admin` وكلمة المرور التي حدّدتها في الخطوة السابقة.

إذا كان أحد الإعدادات الإلزامية مفقودًا، فسيشير إليه `docker compose` صراحةً. للمشكلات الأخرى، راجع [دليل التثبيت](../guide/getting-started.md#启动失败时). وللتفاصيل حول qBittorrent وJackett، راجع [دليل التثبيت](../guide/getting-started.md#同时部署-qbittorrent-或-jackett) و[فهارس التورنت](../guide/indexers.md).

### الطريقة 2: بناء الصورة من الشيفرة المصدرية

تُبنى الصورة محليًا، وهذا مناسب عند الحاجة إلى تعديل الشيفرة المصدرية. وباستثناء مصدر الصورة، فإن الخطوات مطابقة للطريقة 1.

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
docker build -t jav-pilot:local .
cp .env.example .env
```

املأ الملف ‎`.env` كما في الخطوة 3 من الطريقة 1، واضبط `JAV_PILOT_IMAGE` على `jav-pilot:local`، ثم نفّذ `docker compose up -d`. يستغرق البناء الأول وقتًا أطول لأنه ينزّل المتصفح والاعتماديات ويترجم FFmpeg.

تنشر هذه الطريقة JAV Pilot فقط. لنشر qBittorrent أو Jackett معه، راجع [دليل التثبيت](../guide/getting-started.md#方式二从源码构建镜像).

### الطريقة 3: دون Docker

‏JAV Pilot في حد ذاته خدمة ويب، ويعمل مباشرةً على Linux أو macOS أو Windows. المتطلبات المسبقة: Git وPython 3.11 أو أحدث (يُوصى بالإصدار 3.12) وNode.js 24 وnpm، كما تحتاج Debian / Ubuntu إلى الحزمة `python3-venv`. على Linux / macOS:

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
jav-pilot serve
```

ثم افتح `http://127.0.0.1:8766`. تستمع الخدمة افتراضيًا على الجهاز المحلي فقط، ولا تتطلب تسجيل الدخول. لأوامر Windows، راجع [خطوات التثبيت](../guide/getting-started.md#安装步骤)؛ وللوصول من الشبكة المحلية (يتطلب إعداد تسجيل الدخول)، والتشغيل كخدمة systemd، ومجلد مكتبة الوسائط، وتنزيلات الويب، راجع [دليل التثبيت](../guide/getting-started.md#方式三不使用-docker).

### التحديث

**الطريقة 1**: تُنفَّذ في المجلد الذي يحتوي على ملف Compose

```bash
docker compose pull
docker compose up -d
```

**الطريقة 2**: تُنفَّذ في مجلد المستودع

```bash
git pull
docker build -t jav-pilot:local .
docker compose up -d
```

**الطريقة 3**: تُنفَّذ في مجلد المستودع، ثم تُعاد تشغيل الخدمة

```bash
git pull
python -m pip install -e .
npm --prefix frontend ci
npm --prefix frontend run build
```

## 🧭 الإعداد الأولي

1. **تحقّق من توفّر المواقع**: في «المواقع ← تشخيص المواقع»، أدخل رمز كتالوج موجودًا بالفعل، ثم انقر «اختبار كل المواقع». إذا تعذّر الوصول إلى جميع المواقع، فعادةً ما يلزم إعداد وكيل (proxy)؛ راجع [الأسئلة الشائعة](../guide/faq.md#所有站点都连不上).
2. **اتصل بـ qBittorrent موجود** (اختياري): يكون qBittorrent المنشور مع JAV Pilot متصلًا تلقائيًا، فيمكنك تخطّي هذه الخطوة. أما مع qBittorrent موجود مسبقًا، فأدخل عنوانه وحسابه في «الإعدادات ← qBittorrent». وإذا كانت مسارات المجلدات التي يراها qBittorrent تختلف عن تلك التي يراها JAV Pilot، فاضبط مطابقة المسارات كما هو موضح في [دليل الاستخدام](../guide/usage.md#2-连接-qbittorrent可选).
3. **البحث والتنزيل**: أدخل رمز الكتالوج في «البحث»، وافتح تفاصيل العمل، ثم اختر «تنزيل» لرابط magnet أو «بدء تنزيل Web»، وتابع التقدّم في صفحة «التنزيلات».

راجع [دليل الاستخدام](../guide/usage.md) للاطلاع على الشرح الكامل.

## 🌐 المصادر والتكاملات المدعومة

<div dir="rtl">

| الفئة | الدعم |
| --- | --- |
| البيانات الوصفية وروابط magnet | JavBus وJavDB وFC2 (مفعّلة افتراضيًا)؛ FANZA وMGS وAVBase وFC2DB وJAVTEN (يمكن تفعيلها من «المواقع») |
| فهارس التورنت | Sukebei وTokyo Toshokan (عبر Jackett الذي يمكن نشره مع JAV Pilot) |
| تنزيل الفيديو | JableTV وSupJav وMissAV |
| التصنيفات | JavDB وFANZA وFC2 وMGStage وJavMenu، إضافةً إلى المواقع الرسمية لاستوديوهات غير خاضعة للرقابة مثل 1Pondo وCaribbeancom |
| عميل التنزيل | qBittorrent |
| خوادم الوسائط | Jellyfin وEmby وKodi (تقرأ ملفات NFO والملصقات وصور الخلفية) |
| الترجمة بالذكاء الاصطناعي | OpenAI والواجهات المتوافقة معه وAnthropic Claude وGoogle Gemini وAzure OpenAI وOllama وغيرها |
| الإشعارات | Webhook وGotify وTelegram وإشعارات NAS |

</div>

قد تغيّر المواقع الخارجية بنيتها، أو تقيّد الوصول حسب المنطقة، أو تطلب التحقق من البشر. لمعرفة البيانات التي يوفّرها كل موقع ومدى توفّره حاليًا، راجع [وصف المصادر](../guide/sources.md) و«المواقع ← تشخيص المواقع» داخل التطبيق.

## 📸 لقطات الشاشة

<p align="center">
  <img src="../images/detail.png" alt="تفاصيل العمل" width="49%">
  <img src="../images/downloads.png" alt="مهام التنزيل" width="49%">
</p>
<p align="center">
  <img src="../images/library.png" alt="مكتبة الوسائط" width="64%">
  <img src="../images/mobile-search.png" alt="البحث على الجوال" width="16%">
  <img src="../images/mobile-detail.png" alt="تفاصيل العمل على الجوال" width="16%">
  <br>
  <sub>تفاصيل العمل · مهام التنزيل · مكتبة الوسائط · الجوال</sub>
</p>

## 📖 الوثائق

الوثائق مكتوبة باللغة الصينية المبسطة.

<div dir="rtl">

| الاستخدام | التطوير |
| --- | --- |
| [دليل التثبيت](../guide/getting-started.md): طرق التثبيت والمجلدات والوصول عن بُعد | [البنية](../guide/architecture.md) |
| [دليل الاستخدام](../guide/usage.md): من فحص المواقع إلى التنزيل والتنظيم | [واجهة HTTP API](../guide/api.md) |
| [الأسئلة الشائعة](../guide/faq.md) · [الإعدادات](../guide/configuration.md) | [التطوير والتحقق](../guide/development.md) |
| [المصادر](../guide/sources.md) · [فهارس التورنت](../guide/indexers.md) | [الأمان](../../SECURITY.md) |
| [النسخ الاحتياطي والتشغيل](../guide/operations.md) | |

</div>

## 🔒 البيانات والخصوصية

تُحفَظ الإعدادات والسجلات وقواعد البيانات في `data/`‎ داخل مجلد النشر، وتُحفَظ بيانات اعتماد تسجيل الدخول في ‎`.env`؛ فلا تشاركهما مع أحد. لا يتصل JAV Pilot إلا بالمواقع المفعّلة. وفيما عدا ذلك، لا يُرسَل إلى الخدمات الخارجية سوى ما يلي: نص العناوين عند تفعيل ترجمة العناوين (يُرسَل إلى خدمة ترجمة عامة، ويمكن إيقافه)، والعناوين التي تُرسَل إلى خدمة الذكاء الاصطناعي المُعدّة عند النقر على «ترجمة بالذكاء الاصطناعي»، والإشعارات المُعدّة.

## ⚖️ إخلاء المسؤولية

‏JAV Pilot أداة برمجية فقط، ولا يوفّر أي مقاطع فيديو أو حسابات أو موارد، ولا يضمن إمكانية العثور على محتوى معيّن أو تنزيله. لا تنزّل إلا المحتوى الذي يحق لك الحصول عليه، والتزم بالقوانين المحلية وشروط استخدام كل موقع.

## 📄 الترخيص

الشيفرة منشورة بموجب [ترخيص MIT](../../LICENSE). ولا يمنح هذا الترخيص أي حقوق على مقاطع الفيديو أو الصور أو بيانات المواقع أو العلامات التجارية الخاصة بأطراف ثالثة.

## 💬 الملاحظات والدعم

يُرجى إرسال المشكلات والاقتراحات عبر [Issues](https://github.com/drdon1234/JAV-Pilot/issues). ونرحّب بدعم المشروع بنجمة على GitHub.
