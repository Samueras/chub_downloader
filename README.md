# **Chub.ai Card Downloader**

### A simple and user-friendly GUI tool to download character cards from Chub.ai using the API.

![Chub.ai Card Downloader](https://github.com/Samueras/chub_downloader/blob/main/screenshots/gui.png)

## **Table of Contents**
- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
- [Creating Executable](#creating-executable)
- [Configuration](#configuration)
- [Contributing](#contributing)
- [License](#license)

## **Features**
- Download character cards from Chub.ai by simply entering the card name.
- Option to bundle downloaded files as a folder or zip archive.
- Easily set and manage your Chub.ai Token for accessing restricted content.
- HTML reports generated for each card, including descriptions and additional card information.
- Automatically download related gallery images.
- GUI built with **Tkinter** and **ttkbootstrap** for a modern look.
![](https://github.com/Samueras/chub_downloader/blob/main/screenshots/html.png)

## **Installation**

### **Requirements**
1. **Python 3.x**
2. Install the following dependencies:
   ```bash
   pip install requests Pillow markdown ttkbootstrap
   ```

### **Clone the Repository**
```bash
git clone https://github.com/yourusername/chub-card-downloader.git
cd chub-card-downloader
```

## **Usage**

### **Running the Script**
You can run the downloader directly by executing the Python script:
```bash
python chub_card_downloader.py
```

### **Features in the GUI**
1. **Card Name**: Enter the name of the character card you wish to download.
2. **Bundle Option**: Choose whether to download the files as a folder or as a zip archive.
3. **Output Directory**: Select the location where the files will be saved.
4. **Set Chub.ai Token**: (Optional) Add your Chub.ai Token for accessing restricted cards (NSFW/NSFL or private content).

#### **How to Find Your Chub.ai Token**
1. Log in to [Chub.ai](https://chub.ai/).
2. Open the developer tools (usually by pressing `F12`).
3. Navigate to the `Application` tab.
4. Look under **Local Storage** for the `URQL_TOKEN` key.
5. Copy its value and paste it into the Chub.ai Token field in the app.

## **Creating Executable**

You can convert this Python script into a standalone executable using **PyInstaller**.

First, install PyInstaller:
```bash
pip install pyinstaller
```

Then, generate the executable with the following command:
```bash
pyinstaller --onefile --windowed chub_card_downloader.py
```

The `.exe` file will be located in the `dist/` folder.

## **Configuration**

The app automatically creates a `config.ini` file in the same directory as the script. This file stores:
- The last used output directory.
- The bundle option (Folder or Zip).
- Your Chub.ai Token (if provided).

You can modify this file manually if necessary.

## **Contributing**

If you'd like to contribute, please fork the repository and make changes as you'd like. Pull requests are warmly welcome.

1. Fork the repository.
2. Create a new branch: `git checkout -b my-branch-name`.
3. Make your changes.
4. Push to the branch: `git push origin my-branch-name`.
5. Submit a pull request.

## **License**

This project is licensed under the MIT License - see the [[LICENSE](LICENSE)](https://github.com/Samueras/chub_downloader#) file for details.
