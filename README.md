# Oribay Intelligence

## Presentaciones editables (piloto)

La ruta `presentacion` consulta Ventas Oribay o Aplicación Adhesiva con la identidad del usuario. Si la petición menciona expresamente ambos ámbitos, consulta los dos agentes por separado y no crea el archivo si falta evidencia de uno de ellos. Gemini redacta tres secciones con los resultados recibidos y Google Slides crea una presentación editable de seis diapositivas en el Drive personal del usuario. El archivo se basa en la plantilla privada `gs://aad-analitica-oee-oribay-library/templates/oribay-presentation-20261124.pptx`, proporcionada por Oribay, y el enlace queda en la conversación y en Tu espacio. Google Slides API debe estar habilitada en `aad-analitica-oee`; la autorización de Drive existente sirve para crear el documento.

Las páginas de contenido usan tarjetas breves y, cuando el agente devuelve una tabla con una serie numérica de dos a seis filas, un gráfico de barras editable construido exclusivamente con esos valores. Si no hay una serie adecuada, se muestran tarjetas; no se inventan datos para completar un gráfico. El documento requiere revisión humana antes de compartir. El 24/11/2026 se probó una generación privada de seis diapositivas con la cuenta del usuario. La revisión detectó afirmaciones sobre mix de producto y conversión de divisas que la tabla no acreditaba; se añadieron comprobaciones para rechazarlas. La versión visual con gráfico debe probarse antes de promoverla a producción.

En seguimientos como «haz un gráfico visual» o «genera un Google Slides con estos datos», el router recupera la última petición concreta del mismo agente dentro de esa conversación y conserva su alcance. Si el usuario pidió un top 5, una tabla con más de cinco filas se reconsulta y, si persiste el exceso, se bloquea en vez de mostrar un top 10. Una pregunta nueva o un cambio de agente no hereda ese contexto.

## Apariencia corporativa

La interfaz usa la paleta, tipografía Roboto y logotipo oficial de Oribay Group en `static/corporate-theme.css` y `static/oribay-logo.png`. La revisión de Cloud Run `oribay-intelligence-pilot-00088-jas` se publicó el 23/11/2026. El cambio es visual: conserva el enrutador, las conversaciones, las herramientas y las animaciones del chat.

## Estado actual: dos agentes de datos

El chat consulta Ventas Oribay (`agent_33fe6cdf-ec2d-4cd8-93c7-a5c87fc4c345`) y Aplicación Adhesiva (`oee-oribay`) con el OAuth personal del usuario. El router Gemini elige el agente según la petición; PDF y borradores de correo usan el agente elegido. Cada respuesta conserva su origen y el contexto enviado a Conversational Analytics se separa por agente. El mismo chat permite pasar de OEE a facturación. Las alertas programadas siguen disponibles solo para ventas; una alerta de producción se rechaza sin guardar una regla.

El 23/11/2026 se verificaron en la web desplegada una consulta real de OEE de RB4 (ruta Aplicación Adhesiva) y una de facturación de piezas de octubre de 2026 (ruta Ventas Oribay), en la misma conversación. La revisión web `oribay-intelligence-pilot-00076-h2t` recibe el 100 % del tráfico. La consulta de OEE devolvió un desglose por referencia, aunque se preguntó por el OEE de la máquina en conjunto; se añadió una instrucción para exigir el nivel de agregación solicitado, pendiente de comprobar con otra consulta. La prueba no valida todavía PDFs ni correos basados en producción. Las secciones históricas de abajo describen revisiones anteriores y pueden contener estados ya superados.

## Integración de Google Drive en desarrollo

La ruta Drive del chat permite buscar archivos con la cuenta del usuario, leer Google Docs, PDF, CSV y texto, crear carpetas, renombrar y mover archivos, y guardar los PDF de la biblioteca. El router prepara la acción y la conversación muestra la conexión necesaria. Los cambios requieren confirmación. Los demás formatos se abren en Drive. La acción «Usar en el chat» inserta un extracto visible y editable en la caja de conversación.

Las consultas de búsqueda y lectura de Drive separan `path:` de la pregunta, buscan por términos de nombre y contenido, leen los candidatos accesibles y redactan una respuesta basada en ellos con enlaces a los archivos. Si la ruta no se resuelve literalmente, amplían la búsqueda y lo indican. Crear, renombrar, mover y guardar un PDF siguen requiriendo revisión y confirmación.

El flujo conversacional de Drive se desplegó en Cloud Run en la revisión `oribay-intelligence-pilot-00074-9fr`. Drive API está habilitada en `aad-analitica-oee`. El OAuth personal solicita además el ámbito `https://www.googleapis.com/auth/drive`; el usuario ya observó la conexión y lectura en producción. Queda por comprobar la respuesta directa de búsqueda y lectura de esta revisión con sus consultas reales.

Desplegado en Cloud Run. El servicio `oribay-intelligence-pilot` ejecuta la app con IAP y almacén privado; `oribay-alerts-worker` procesa alertas programadas.

Chat paralelo con cuatro rutas visibles. El router Gemini decide entre consulta comercial, borrador de alerta, PDF y borrador de correo. La consulta comercial usa la API A2A del agente Ventas Oribay y renderiza los artefactos Vega-Lite; el SQL queda en un panel secundario.

## Router y memoria (piloto)

El router separa la intención de herramienta (`consulta`, `pdf`, `presentacion`, `correo`, `alerta`, `drive`) del agente de datos. El historial reciente del mismo agente se ofrece al router para decidir la ruta; solo las preguntas que hacen referencia a resultados anteriores envían contexto al agente de datos. Cada nueva respuesta guarda metadatos acotados de su consulta de origen (agente, años, referencias y límite de `top N`) en Firestore para resolver seguimientos posteriores. Un cambio explícito de dominio inicia un tramo nuevo de memoria. Las pruebas de regresión cubren el top 5, el gráfico, el PDF de OEE y el desglose comercial de zamak/acero inoxidable.

Esta fase todavía usa los dos agentes configurados en código. El catálogo administrable, la selección por permisos antes del router y la evaluación con un conjunto amplio de conversaciones quedan para las siguientes fases.

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
