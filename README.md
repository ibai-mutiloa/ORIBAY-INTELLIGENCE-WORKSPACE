# Oribay Intelligence

## Integración de Google Drive en desarrollo

El panel «Tu espacio» permite buscar archivos de Drive con la cuenta del usuario, leer Google Docs, PDF, CSV y texto, crear carpetas, renombrar y mover archivos, y guardar los PDF de la biblioteca. Los cambios requieren confirmación. Los demás formatos se abren en Drive. La acción «Usar en el chat» inserta un extracto visible y editable en la caja de conversación.

Esta integración se desplegó en Cloud Run en la revisión `oribay-intelligence-pilot-00071-sbt`. Drive API está habilitada en `aad-analitica-oee`. El OAuth personal solicita además el ámbito `https://www.googleapis.com/auth/drive`; los usuarios deberán volver a conectar Google desde el enlace del sitio. Queda por comprobar el consentimiento con una cuenta real y las operaciones con archivos reales.

Desplegado en Cloud Run. El servicio `oribay-intelligence-pilot` ejecuta la app con IAP y almacén privado; `oribay-alerts-worker` procesa alertas programadas.

Chat paralelo con cuatro rutas visibles. El router Gemini decide entre consulta comercial, borrador de alerta, PDF y borrador de correo. La consulta comercial usa la API A2A del agente Ventas Oribay y renderiza los artefactos Vega-Lite; el SQL queda en un panel secundario.

## Estado

- **Ventas:** consulta real al agente y gráfico renderizado en navegador.
- **Router:** decisión Gemini verificada para una petición de alerta; si Gemini falla, utiliza reglas simples y lo declara en la traza.
- **Contexto conversacional:** conserva hasta cinco análisis de ventas en memoria del navegador y los pasa al router. Una petición posterior como «avísame cuando esta facturación...» puede heredar métrica, periodo y filtros del análisis más reciente; la tarjeta indica qué contexto se usó. Se verificó la herencia de «piezas sin portes» en una regla mensual.
- **Alertas:** se guardan por usuario en Firestore, permiten comprobación manual y activación expresa. El worker programado consulta las reglas activas y notifica al usuario cuando cruza el umbral.
- **PDF:** genera informes con resumen, tablas e imágenes de los gráficos; se guardan en Cloud Storage privado.
- **Correo:** el borrador se revisa en la interfaz y el usuario confirma destinatario, remitente y asunto antes de enviar por Gmail.
- **Despliegue:** Cloud Run con IAP; el historial y la biblioteca son privados por usuario.

## Arranque local

Instalar `requirements.txt`. Usar credenciales ADC autorizadas o, para una prueba temporal, definir `ORIBAY_TEST_ACCESS_TOKEN` con un token de corta duración de `gcloud auth print-access-token`. Ejecutar `python app.py` y abrir `http://127.0.0.1:8080/`. El servicio escucha por defecto solo en `127.0.0.1`.

El token temporal vence aproximadamente en una hora. Las librerías de gráficos se cargan desde jsDelivr: para producción deben empaquetarse con la aplicación. Antes de desplegar son necesarios identidad de servicio, acceso restringido de usuarios, persistencia administrada, validación de métricas y controles de coste.

El indicador visual del router y de Ventas Oribay corresponde a dos llamadas reales separadas. La interfaz no muestra razonamiento interno del modelo. El correo sugerido por variable de entorno solo es válido para esta prueba local; en producción debe proceder de un inicio de sesión validado en servidor.

En modo local, el historial vive en la pestaña. En el despliegue de Cloud Run, las conversaciones se guardan por usuario en Firestore.

## Biblioteca privada por usuario

Análisis completados, alertas creadas y borradores de correo se registran en Firestore bajo la identidad autenticada por IAP. Los PDF se guardan en un bucket privado y pueden volver a descargarse desde Documentos. El menú permite filtrar por tipo, abrir análisis con sus gráficos/tablas, y retirar elementos de la biblioteca; las descargas y cambios comprueban la propiedad en el servidor.

## Conversaciones persistentes

La revisión 00014 añade un historial privado de chats en la barra lateral. Cada conversación y mensaje se guarda en Firestore bajo la identidad validada por IAP, se puede reabrir para continuar con contexto reciente o eliminar desde el menú. El borrado de un chat elimina sus mensajes; los documentos, alertas y borradores guardados en la biblioteca se conservan por separado. Los chats nuevos reutilizan el router actual y pueden dirigirse a ventas, informes, alertas y correo.

## Herramientas ampliadas (revisión 00011)

- **Correos:** el router redacta asunto y cuerpo en español profesional a partir de hechos devueltos por Ventas Oribay. El borrador se puede editar y guardar en la biblioteca. El usuario puede enviar desde su propio buzón de Workspace; la interfaz confirma remitente, destinatario y asunto antes de llamar a Gmail API. Las credenciales proceden de la cuenta de servicio dedicada, autorizada mediante delegación de dominio y scope `gmail.send`.
- **PDF:** informe con resumen, tablas, imágenes de gráficos, fecha, fuente y páginas numeradas. Se guarda en el bucket privado y queda disponible en Documentos por propietario.
- **Alertas:** borradores privados editables, pausa/retirada, comprobación manual con extracción estricta de una única celda numérica, y activación explícita por el usuario. Las reglas activas se evalúan por un Cloud Run Job cada cinco minutos; el worker evita ejecuciones solapadas, conserva última comprobación/errores y reintenta fallos con espera. Solo notifica al cruzar el umbral y vuelve a permitir otro aviso cuando la métrica baja y vuelve a cruzarlo.
- **Correo y alertas:** Workspace confirmó la autorización de delegación Gmail para `oribay-email-sender@aad-analitica-oee.iam.gserviceaccount.com` (cliente OAuth `118436371552674187930`). Cloud Run y el worker usan esa cuenta dedicada mediante impersonación. La renovación del token Gmail para `ibai.mutiloa@oribay.com` se verificó correctamente sin enviar mensajes. El envío manual requiere confirmación en la interfaz; las alertas requieren activación explícita.
- **Privacidad:** IAP valida las peticiones del chat; las alertas y su historial pertenecen al usuario autenticado. El scheduler solo invoca el trabajo programado mediante IAM, y el worker procesa únicamente documentos Firestore con `status=active`.


## Operación del evaluador de alertas

El job `oribay-alerts-worker` se ejecuta cada cinco minutos desde Cloud Scheduler (`Europe/Madrid`). Se desplegó en `europe-west1` con la cuenta `oribay-intelligence-pilot@aad-analitica-oee.iam.gserviceaccount.com`. Las frecuencias admitidas son diaria, semanal y mensual; activar una alerta exige confirmación y la primera comprobación ocurre en el siguiente ciclo. Una respuesta con varias filas o sin una columna de valor inequívoca se considera fallida y no envía notificaciones.

La delegación Gmail está autorizada para `oribay-email-sender@aad-analitica-oee.iam.gserviceaccount.com` (cliente OAuth `118436371552674187930`). La aplicación y el worker usan esta cuenta dedicada mediante impersonación. La renovación del token para `ibai.mutiloa@oribay.com` se verificó correctamente sin enviar mensajes. El envío manual requiere confirmación; las alertas requieren activación expresa.

La revisión web estable activa es `oribay-intelligence-pilot-00043-k8f` y mantiene IAP habilitado; el worker `oribay-alerts-worker` usa la imagen `oauth-20261118`. El cliente OAuth interno y la clave de cifrado están guardados en Secret Manager con acceso para la cuenta runtime. Las variables y secretos OAuth se retiraron de la revisión web activa tras comprobar que IAP intercepta el inicio y el callback OAuth; las credenciales permanecen en Secret Manager. El worker conserva su cuenta runtime actual y obtiene las credenciales Gmail mediante la cuenta de envío dedicada.

La cuenta runtime no tiene `roles/bigquery.dataViewer` en la vista de ventas. El diseño de OAuth de usuario mediante el servidor MCP oficial de Conversational Analytics está preparado localmente y el trabajador de alertas contiene el adaptador correspondiente, pero las consultas de producción todavía no usan permisos personales: IAP intercepta el flujo OAuth antes de que la aplicación reciba el consentimiento o el callback. No se debe volver a conceder lectura de la vista a la cuenta runtime como sustituto.

El flujo preparado guarda únicamente el token de renovación cifrado en Firestore; el worker usa ese token del propietario de cada alerta para consultar el agente. El endpoint MCP devuelve la conversación y artefactos del agente; el adaptador conserva respuesta, tablas, SQL y especificaciones Vega cuando estén presentes. Esta extracción requiere verificación contra una respuesta real autorizada antes de activar producción. El contacto Lander Fraga se resuelve al correo verificado `lander.fraga@oribay.com`.
