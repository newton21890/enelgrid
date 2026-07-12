# Home Assistant - enelgrid Integration (Fork con correzioni)

Questo è un **fork** dell'integrazione [sathia-musso/enelgrid](https://github.com/sathia-musso/enelgrid) con correzioni di bug critici che impedivano il funzionamento corretto con le versioni recenti di Home Assistant.

## ❗ Bug corretti rispetto all'originale

| Bug | Sintomo | Fix |
|-----|---------|-----|
| **Mancanza di `unique_id`** nei sensori | *"Questa entità non ha un ID univoco, pertanto le sue impostazioni non possono essere gestite dall'interfaccia utente"* | Aggiunto `_attr_unique_id` a entrambi i sensori |
| **Uso di API deprecata `async_add_external_statistics`** senza `mean_type` | Dati non importati nella Energy Dashboard (Home Assistant ≥2026.7) | Sostituito con `async_import_statistics` + parametro `mean_type="sum"` |
| **Blocking `open()` dentro l'event loop** | Warning *"Detected blocking call to open inside the event loop"* | Offload della scrittura file con `asyncio.to_thread` |
| **Entità sensore consumi non allineata** | Impossibile trovare il sensore nella Energy Dashboard | Aggiunto `entity_id` esplicito e `unique_id` basato sul POD |

## 📋 Features

- 📊 Automatic login using Enel's SAML authentication process.
- 🔄 **Supports re-authentication** if the login process encounters issues.
- 🕒 Fetches and tracks hourly energy consumption (Enel provides data with a three-day delay).
- 📈 Tracks monthly cumulative consumption.
- ✅ Seamless integration with Home Assistant Energy Dashboard.
- 🔁 Automatically updates data daily.

## 🛠️ Installation

### Manual Installation

1. Copy the **`enelgrid`** folder into:
    ```
    config/custom_components/enelgrid
    ```
2. Restart Home Assistant.
3. In Home Assistant, go to:  
   **Settings → Devices & Services → Add Integration**  
   Search for **"enelgrid"**.
4. Enter your Enel login credentials and POD details.

### Installation via HACS (Recommended)

1. In **HACS**, go to **Integrations**.
2. Add this repository as a **Custom Repository**:  
   `https://github.com/newton21890/enelgrid`
3. Search for **"enelgrid"** and install.
4. Restart Home Assistant.
5. Follow the setup steps in **Settings → Devices & Services**.

## ⚙️ Configuration

During setup, you'll need to provide:

- **Username** Your Enel account email
- **Password**
- **POD Number** Found on your Enel bill
- **User Number** Also found on your Enel bill
- **Price per Kwh** - Take your total electricity bill amount and divide it by the total kWh consumed (as shown on your bill) to get a reasonable estimate for the price per kWh

These credentials are stored securely in Home Assistant's `config_entries` storage.

## Configurazione Energy Dashboard

L'integrazione crea:

- **`sensor.enelgrid_{POD}_monthly_consumption`** — valore cumulativo mensile (per un colpo d'occhio)
- **`sensor:enelgrid_{POD}_consumption`** — statistiche orarie importate per la Energy Dashboard (non è un sensore fisico, appare solo nel selettore entità)

Per configurare la Energy Dashboard:

1. Vai in **Settings → Energy**
2. In **Grid consumption** clicca **Add consumption**
3. Cerca **`sensor:enelgrid_{POD}_consumption`** (o digita manualmente l'ID) — apparirà come "Enel {POD} Consumption"
4. Selezionalo e salva

**Nota importante:** Enel fornisce i dati con **circa 3 giorni di ritardo**. Il grafico del giorno corrente sarà sempre vuoto. Per vedere i dati, seleziona un intervallo di 2-3 giorni fa nel grafico della Energy Dashboard.

![Description of Image](assets/energy_config.jpg)

if all goes well, you should see something like this:

![Description of Image](assets/example.jpg)

## 🕒 Automatic Data Fetching

- Data is automatically fetched every day.
- Data is also fetched immediately upon first installation.

## 🏷️ Supported Features

| Feature                            | Status |
|------------------------------------|--------|
| Hourly Energy Data                 | ✅ |
| Daily Energy Data                  | ✅ |
| Monthly Cumulative Sensor          | ✅ |
| Energy Dashboard Integration       | ✅ |
| Automatic Login                    | ✅ |
| Automatic Daily Fetch              | ✅ |
| Re-authentication Support          | ✅ |

## 🔗 Links

- 📖 [Enel Portal](https://www.enel.it/)
- 📘 [Home Assistant Docs](https://www.home-assistant.io/integrations/)
- 🔧 [Repository originale](https://github.com/sathia-musso/enelgrid)

## 🧑‍💻 Credits

- Integrazione originale di [Sathia Francesco Musso](https://github.com/sathia-musso/enelgrid/)
- Fork con correzioni di [@newton21890](https://github.com/newton21890/enelgrid)
- Contributi da [@shatteringlass](https://github.com/shatteringlass/enelgrid) (refactoring parser + offset handling)
- Contributi da [@LukeRPi](https://github.com/LukeRPi/enelgrid) (fix statistiche mensili)

---

## 📜 License

This project is licensed under the MIT License.
