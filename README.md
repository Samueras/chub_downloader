# Chub.ai Card Downloader

A simple yet powerful GUI application for downloading character cards from [Chub.ai](https://chub.ai/).

![GUI Screenshot](https://github.com/Samueras/chub_downloader/blob/main/screenshots/gui.png)

## **Usage (Recommended)**

For most users, the simplest way to use this tool is by downloading the standalone executable (`.exe`) from the project's **Releases** page.

1.  Go to the [Releases Page](https://github.com/Samueras/chub_downloader/releases).
2.  Download the latest `chub_card_downloader.exe` file.
3.  Run the application. No installation is needed.

## **Features**

-   **Download by URL or Path**: Directly download a card using its full Chub.ai URL or its unique path (e.g., `p1a_gura`).
-   **Simple & Advanced Search**: A powerful search window lets you find cards by name or with a wide range of filters, including:
    -   Sorting by popularity, creation date, rating, and more.
    -   Filtering by tags, creator, token count, and age.
    -   Quick presets for "Latest", "Trending", and "Recent Hits".
-   **Detailed Output**: Saves all card assets, including the main image, gallery images, and character data (`.json`).
-   **HTML Overview**: Generates an HTML file for a quick and comprehensive offline overview of the card.
-   **Token Support**: Add your Chub.ai API token to access restricted or private content.
-   **Persistent Settings**: Remembers your output directory, bundle option, and advanced search filters between sessions.

![HTML Report Screenshot](https://github.com/Samueras/chub_downloader/blob/main/screenshots/html.png)

## **Setting Your Chub.ai Token**

To access restricted cards (NSFW/NSFL) or private content, you may need to provide your API token.

1.  Click the **Set Chub.ai Token** button in the app.
2.  Log in to [Chub.ai](https://chub.ai/).
3.  Open your browser's developer tools (usually `F12`).
4.  Navigate to the `Application` tab -> `Local Storage` -> `chub.ai`.
5.  Find the `URQL_TOKEN` key, copy its value, and paste it into the token field in the app.

## **Usage (for Developers)**

If you want to run the script directly or modify it, follow these steps.

### **Installation**

Clone the repository and install the dependencies:
```bash
git clone https://github.com/Samueras/chub_downloader.git
cd chub_downloader
pip install -r requirements.txt
```

### **Running the Script**

```bash
python chub_card_downloader.py
```

### **Hourly Stats Dashboard (LAN Web UI)**

This repository also includes a lightweight stats service that:
- polls Chub.ai once per hour for your cards,
- stores time-series data in SQLite,
- serves a local web dashboard with charts and avatars (using avatar URLs directly).

#### **Run**
```bash
python chub_stats_server.py
```

#### **Configure**
The first run creates `stats_config.ini`. Edit it with your values:
```
[Stats]
api_token =
creator = sambolic
poll_seconds = 3600
host = 0.0.0.0
port = 8787
db_path = d:\Windsurf\chub_downloader\stats.db
```

`creator` can be either your numeric creator ID or your username. If you use a username, the service filters by fullPath prefix (e.g. `username/card-name`).

You can also override settings via environment variables:
- `CHUB_API_TOKEN`
- `CHUB_CREATOR`
- `CHUB_POLL_SECONDS`
- `CHUB_HOST`
- `CHUB_PORT`
- `CHUB_DB_PATH`

Open the dashboard in your LAN browser at `http://<server-ip>:8787/`.

### **Forks Scanner (Standalone Tool)**

This repository also includes `ForksScanner.html`, a standalone, dependency-free browser tool that scans a Chub.ai user's characters and finds all forks of each one. It is unrelated to the downloader itself and runs entirely in the browser — just open the file directly.

- Enter a target username (defaults to `Sambolic`) and click **Start Scan**.
- It paginates through the user's characters, then fetches forks for each one in series.
- Includes a CORS-proxy toggle (on by default) to avoid browser CORS errors, and automatic retry with exponential backoff on HTTP 429 rate-limit responses.
- Results render inline and can be exported.

### **Creating an Executable**

This project uses **PyInstaller** to create the standalone executable.

First, install PyInstaller:
```bash
pip install pyinstaller
```

Then, generate the executable:
```bash
pyinstaller --onefile --windowed chub_card_downloader.py
```

The `.exe` file will be located in the `dist/` folder.

## **Configuration**

The app automatically creates a `config.ini` file to store your settings, including your last used output directory, bundle option, API token, and advanced search filters.

## **Support**

If you find this tool useful, please consider supporting its development:

[<img src="https://storage.ko-fi.com/cdn/kofi2.png?v=3" alt="Ko-fi" height="36">](https://ko-fi.com/samueras)

## **Contributing**

Contributions are welcome! Please feel free to submit a pull request or open an issue.

## **License**

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
