<p align="center">
  <img src="../images/banner_es.svg" alt="JAV Pilot" width="100%">
</p>

<div align="center">

<a href="../../README.md">简体中文</a> |
<a href="README_zh-TW.md">繁體中文</a> |
<a href="README_en.md">English</a> |
<a href="README_ja.md">日本語</a> |
<a href="README_fr.md">Français</a> |
<b>Español</b> |
<a href="README_ru.md">Русский</a> |
<a href="README_ar.md">العربية</a>

<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/v/drdon1234/jav-pilot?sort=semver&label=docker&color=3567b7" alt="Versión de Docker"></a>
<a href="https://hub.docker.com/r/drdon1234/jav-pilot"><img src="https://img.shields.io/docker/pulls/drdon1234/jav-pilot?color=3567b7" alt="Descargas de Docker"></a>
<img src="https://img.shields.io/badge/platform-linux%2Famd64-3567b7" alt="Plataforma">
<img src="https://img.shields.io/badge/python-3.11+-3567b7" alt="Python 3.11+">
<a href="../../LICENSE"><img src="https://img.shields.io/badge/license-MIT-3567b7" alt="Licencia MIT"></a>
<br>

<a href="../guide/getting-started.md">Instalación</a> |
<a href="../guide/usage.md">Uso</a> |
<a href="../guide/faq.md">Preguntas frecuentes</a> |
<a href="../guide/configuration.md">Configuración</a> |
<a href="https://github.com/drdon1234/JAV-Pilot/issues">Informar de un problema</a>

</div>

<br>

JAV Pilot es una aplicación autoalojada para buscar, descargar y gestionar una biblioteca de vídeos. Se ejecuta en un NAS, un servidor Linux o un ordenador personal. Agrega los resultados de varios sitios de metadatos, descarga mediante qBittorrent o directamente desde sitios de vídeo y, al terminar cada descarga, la archiva en la biblioteca multimedia con pósteres y metadatos NFO que Jellyfin, Emby y Kodi reconocen directamente. Toda la configuración, los registros y los archivos multimedia permanecen en su propio equipo, y la interfaz web funciona en navegadores de escritorio y móviles.

> [!NOTE]
> La documentación detallada de la carpeta `docs/` solo está disponible actualmente en chino simplificado. La interfaz web está disponible en español y sigue por defecto el idioma del navegador y del sistema.

<p align="center">
  <img src="../images/search.png" alt="Resultados de búsqueda de JAV Pilot" width="92%">
  <br>
  <sub>Todas las capturas de pantalla usan datos de ejemplo ficticios.</sub>
</p>

## ✨ Funciones

1. 🔍 **Búsqueda multisitio**: consulta en paralelo JavBus, JavDB, FC2 y otros sitios de metadatos, combina los resultados de una misma obra por código de catálogo (番号) y conserva el origen de cada campo.
2. 🧲 **Agregación de enlaces magnet**: muestra el tamaño, el origen y la información de subtítulos, sin duplicados gracias al info hash; Sukebei y Tokyo Toshokan pueden añadirse mediante Jackett.
3. ⬇️ **Dos canales de descarga**: los enlaces magnet se envían a qBittorrent; las descargas web seleccionan la calidad automáticamente y priorizan, en este orden, la versión original, la subtitulada en chino y la sin censura.
4. 🔁 **Detección de duplicados**: antes de crear una tarea se comprueban las tareas de qBittorrent, las descargas web y la biblioteca para evitar descargar dos veces la misma obra.
5. 🗂️ **Organización automática**: las descargas completadas se archivan por código de catálogo y se generan pósteres, imágenes de fondo y metadatos NFO.
6. 📚 **Gestión de la biblioteca**: analiza la carpeta de vídeos, incluidos los archivos añadidos manualmente, y completa los pósteres y metadatos que faltan.
7. 🏆 **Clasificaciones**: clasificaciones de obras, actrices y categorías de JavDB, FANZA, FC2, MGStage y otras fuentes, con análisis de fichas por lotes en segundo plano.
8. 🌐 **Traducción de títulos**: servicio de traducción público integrado y compatibilidad con servicios de IA como las API compatibles con OpenAI, Claude, Gemini y Ollama; las traducciones usan por defecto el idioma de la interfaz.
9. 🩺 **Diagnóstico de sitios**: comprueba periódicamente la disponibilidad de los sitios y distingue fallos de DNS, de conexión, de TLS, de verificación antibots y de cambios en la estructura de las páginas.
10. 🔔 **Notificaciones**: envía avisos mediante Webhook, Gotify, Telegram o las notificaciones del NAS cuando una descarga termina o falla, queda poco espacio en disco o un sitio falla.
11. 📱 **Interfaz multilingüe y adaptable**: disponible en 简体中文, 繁體中文, English, 日本語, Français, Español, Русский y العربية, y sigue por defecto el idioma del navegador y del sistema; funciona en escritorio y en móviles, con temas claro y oscuro.

## 🚀 Inicio rápido

Hay tres métodos de instalación. Para un primer despliegue se recomienda el método 1.

| Método | Caso de uso | Requisitos previos |
| --- | --- | --- |
| [Método 1: Docker (recomendado)](#método-1-despliegue-con-docker-recomendado) | NAS, servidor Linux o WSL2 en Windows | Docker y Docker Compose v2 |
| [Método 2: compilar desde el código fuente](#método-2-compilar-la-imagen-desde-el-código-fuente) | Modificar el código fuente o compilar una imagen propia | Docker, Git |
| [Método 3: sin Docker](#método-3-sin-docker) | Sin Docker disponible, o ejecución como servicio del sistema | Git, Python 3.11+, Node.js 24 |

### Método 1: despliegue con Docker (recomendado)

Utiliza la imagen publicada en Docker Hub. Solo se necesitan un archivo `docker-compose.yml` y un archivo `.env`.

> [!NOTE]
> La imagen solo es compatible con la arquitectura x86_64 (amd64). En Windows, ejecute los siguientes pasos dentro de WSL2.

**1. Elegir un archivo Compose**

Cada archivo Compose despliega los siguientes servicios:

| Archivo Compose | Servicios desplegados |
| --- | --- |
| `docker-compose.yml` | JAV Pilot |
| `deploy/docker-compose.jackett.yml` | JAV Pilot, Jackett |
| `deploy/docker-compose.qbittorrent.yml` | JAV Pilot, qBittorrent |
| `deploy/docker-compose.full.yml` | JAV Pilot, qBittorrent, Jackett |

qBittorrent es un cliente BitTorrent; Jackett proporciona la interfaz Torznab para Sukebei y Tokyo Toshokan. Los contenedores que se despliegan junto con JAV Pilot se llaman `jav-pilot-qbittorrent` y `jav-pilot-jackett`, por lo que no entran en conflicto con contenedores existentes del mismo tipo.

**2. Descargar el archivo Compose y la plantilla de configuración**

```bash
mkdir jav-pilot && cd jav-pilot
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/drdon1234/JAV-Pilot/main/.env.example
```

Si en el paso 1 eligió un archivo de la carpeta `deploy/`, sustituya `main/docker-compose.yml` en el segundo comando por su ruta (por ejemplo `main/deploy/docker-compose.full.yml`). El archivo guardado conserva el nombre `docker-compose.yml`.

**3. Completar `.env`**

Ejecute el siguiente comando para generar la clave de sesión y establecer la identidad de ejecución (`PUID` / `PGID`) en la cuenta actual:

```bash
sed -i "s/^JAV_PILOT_AUTH_SECRET=.*/JAV_PILOT_AUTH_SECRET=$(openssl rand -hex 32)/; s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

A continuación, edite `.env` (por ejemplo con `nano .env`):

- `JAV_PILOT_AUTH_PASSWORD`: contraseña de inicio de sesión, de al menos 12 caracteres. Obligatoria.
- `QBITTORRENT_PASSWORD`: necesaria si qBittorrent se despliega junto con JAV Pilot, de al menos 6 caracteres. Se convierte en la contraseña de la interfaz web de qBittorrent (usuario `admin`), y JAV Pilot se conecta automáticamente con las mismas credenciales.
- Carpetas: la biblioteca multimedia se encuentra por defecto en `media/` dentro de la carpeta actual. Para usar otra carpeta del NAS, modifique la sección «目录» (Carpetas) de `.env`.

**4. Iniciar el servicio**

```bash
docker compose up -d
```

El primer inicio descarga una imagen de aproximadamente 1,1 GB. Después, abra `http://<IP del host>:8766` en un navegador e inicie sesión con el usuario `admin` y la contraseña definida en el paso anterior.

Si falta un ajuste obligatorio, `docker compose` lo indica de forma explícita. Para otros problemas, consulte la [guía de instalación](../guide/getting-started.md#启动失败时). Para más información sobre qBittorrent y Jackett, consulte la [guía de instalación](../guide/getting-started.md#同时部署-qbittorrent-或-jackett) y los [indexadores de torrents](../guide/indexers.md).

### Método 2: compilar la imagen desde el código fuente

Compila la imagen localmente, para los casos en que haya que modificar el código fuente. Salvo el origen de la imagen, los pasos son los mismos que en el método 1.

```bash
git clone https://github.com/drdon1234/JAV-Pilot.git
cd JAV-Pilot
docker build -t jav-pilot:local .
cp .env.example .env
```

Complete `.env` como en el paso 3 del método 1, establezca `JAV_PILOT_IMAGE` en `jav-pilot:local` y ejecute `docker compose up -d`. La primera compilación descarga el navegador y las dependencias y compila FFmpeg, por lo que tarda un tiempo.

Este método despliega solo JAV Pilot. Para desplegar también qBittorrent o Jackett, consulte la [guía de instalación](../guide/getting-started.md#方式二从源码构建镜像).

### Método 3: sin Docker

JAV Pilot es en sí mismo un servicio web y se ejecuta directamente en Linux, macOS o Windows. Requisitos previos: Git, Python 3.11 o posterior (se recomienda 3.12), Node.js 24 y npm; en Debian / Ubuntu también se necesita `python3-venv`. En Linux / macOS:

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

Después, abra `http://127.0.0.1:8766`. Por defecto el servicio solo escucha en la máquina local y no requiere inicio de sesión. Para los comandos en Windows, consulte los [pasos de instalación](../guide/getting-started.md#安装步骤); para el acceso desde la red local (requiere configurar el inicio de sesión), la ejecución como servicio de systemd, la carpeta de la biblioteca y las descargas web, consulte la [guía de instalación](../guide/getting-started.md#方式三不使用-docker).

### Actualización

**Método 1**: ejecutar en la carpeta que contiene el archivo Compose

```bash
docker compose pull
docker compose up -d
```

**Método 2**: ejecutar en la carpeta del repositorio

```bash
git pull
docker build -t jav-pilot:local .
docker compose up -d
```

**Método 3**: ejecutar en la carpeta del repositorio y reiniciar después el servicio

```bash
git pull
python -m pip install -e .
npm --prefix frontend ci
npm --prefix frontend run build
```

## 🧭 Configuración inicial

1. **Comprobar la disponibilidad de los sitios**: en «Sitios → Diagnóstico de sitios», introduzca un código de catálogo que exista y haga clic en «Probar todos los sitios». Si no se puede acceder a ningún sitio, normalmente es necesario configurar un proxy; consulte las [preguntas frecuentes](../guide/faq.md#所有站点都连不上).
2. **Conectar un qBittorrent existente** (opcional): un qBittorrent desplegado junto con JAV Pilot ya está conectado, por lo que puede omitir este paso. Para un qBittorrent existente, introduzca su dirección y su cuenta en «Ajustes → qBittorrent». Si qBittorrent y JAV Pilot ven rutas de carpetas distintas, configure la correspondencia de rutas según la [guía de uso](../guide/usage.md#2-连接-qbittorrent可选).
3. **Buscar y descargar**: introduzca un código de catálogo en «Buscar», abra la ficha de la obra, elija «Descargar» en un enlace magnet o «Iniciar descarga web» y siga el progreso en la página «Descargas».

Consulte la [guía de uso](../guide/usage.md) para el procedimiento completo.

## 🌐 Fuentes e integraciones compatibles

| Categoría | Compatibilidad |
| --- | --- |
| Metadatos y enlaces magnet | JavBus, JavDB, FC2 (activados por defecto); FANZA, MGS, AVBase, FC2DB, JAVTEN (se pueden activar en «Sitios») |
| Indexadores de torrents | Sukebei, Tokyo Toshokan (mediante Jackett, que puede desplegarse junto con JAV Pilot) |
| Descargas de vídeo | JableTV, SupJav, MissAV |
| Clasificaciones | JavDB, FANZA, FC2, MGStage, JavMenu y los sitios oficiales de estudios sin censura como 1Pondo y Caribbeancom |
| Cliente de descargas | qBittorrent |
| Servidores multimedia | Jellyfin, Emby, Kodi (leen NFO, pósteres e imágenes de fondo) |
| Traducción con IA | OpenAI y API compatibles, Anthropic Claude, Google Gemini, Azure OpenAI, Ollama, etc. |
| Notificaciones | Webhook, Gotify, Telegram, notificaciones del NAS |

Los sitios externos pueden cambiar su estructura, restringir el acceso por región o exigir verificación antibots. Para conocer los datos que ofrece cada sitio y su disponibilidad actual, consulte la [descripción de las fuentes](../guide/sources.md) y «Sitios → Diagnóstico de sitios» en la aplicación.

## 📸 Capturas de pantalla

<p align="center">
  <img src="../images/detail.png" alt="Ficha de una obra" width="49%">
  <img src="../images/downloads.png" alt="Tareas de descarga" width="49%">
</p>
<p align="center">
  <img src="../images/library.png" alt="Biblioteca multimedia" width="64%">
  <img src="../images/mobile-search.png" alt="Búsqueda en móvil" width="16%">
  <img src="../images/mobile-detail.png" alt="Ficha de una obra en móvil" width="16%">
  <br>
  <sub>Ficha de una obra · Tareas de descarga · Biblioteca multimedia · Móvil</sub>
</p>

## 📖 Documentación

La documentación está escrita en chino simplificado.

| Uso | Desarrollo |
| --- | --- |
| [Guía de instalación](../guide/getting-started.md): métodos de instalación, carpetas y acceso remoto | [Arquitectura](../guide/architecture.md) |
| [Guía de uso](../guide/usage.md): desde la comprobación de sitios hasta la descarga y la organización | [API HTTP](../guide/api.md) |
| [Preguntas frecuentes](../guide/faq.md) · [Configuración](../guide/configuration.md) | [Desarrollo y verificación](../guide/development.md) |
| [Fuentes](../guide/sources.md) · [Indexadores de torrents](../guide/indexers.md) | [Seguridad](../../SECURITY.md) |
| [Copias de seguridad y operación](../guide/operations.md) | |

## 🔒 Datos y privacidad

La configuración, los registros y las bases de datos se guardan en `data/` dentro de la carpeta de despliegue, y las credenciales de inicio de sesión en `.env`; no comparta ninguno de ellos. JAV Pilot solo accede a los sitios activados. Aparte de eso, solo se envía lo siguiente a servicios externos: el texto de los títulos cuando la traducción de títulos está activada (se envía a un servicio de traducción público y puede desactivarse), los títulos enviados al servicio de IA configurado al hacer clic en «Traducir con IA» y las notificaciones configuradas.

## ⚖️ Aviso legal

JAV Pilot es una herramienta de software. No proporciona vídeos, cuentas ni recursos, y no garantiza que se pueda encontrar o descargar ningún contenido concreto. Descargue únicamente contenidos que tenga derecho a obtener y respete la legislación local y las condiciones de uso de cada sitio.

## 📄 Licencia

El código se publica bajo la [licencia MIT](../../LICENSE). Esta licencia no concede ningún derecho sobre vídeos, imágenes, datos de sitios ni marcas de terceros.

## 💬 Comentarios y apoyo

Comunique problemas y sugerencias a través de [Issues](https://github.com/drdon1234/JAV-Pilot/issues). Las estrellas en el repositorio son bienvenidas.
