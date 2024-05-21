import os
import webbrowser
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

# Set your app key, app secret, and redirect URI
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI")

# Construct the authorization URL with token_access_type=offline
auth_url = "https://www.dropbox.com/oauth2/authorize?" + urlencode({
    "client_id": APP_KEY,
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
    "token_access_type": "offline"
})

# Open the authorization URL in the browser
print("Open this URL in a client web browser:")
print(auth_url)
webbrowser.open(auth_url)

# Get http://localhost?code=XXXXXXX.  That XXXXXXX is the DROPBOX_REFRESH_TOKEN
