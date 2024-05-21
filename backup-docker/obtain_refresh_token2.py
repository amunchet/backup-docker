import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
load_dotenv()

APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI")
ACCESS_CODE = input("Enter access code (would be http://...?code=XXXX):")

# Dropbox token URL
token_url = "https://api.dropboxapi.com/oauth2/token"

# Payload for the POST request
data = {
    'code': ACCESS_CODE,
    "redirect_uri": REDIRECT_URI,
    'grant_type': 'authorization_code'
}

# Perform the POST request with basic authentication
response = requests.post(token_url, 
                        data=data, 
                        auth=HTTPBasicAuth(APP_KEY, APP_SECRET), 
                        headers={'Content-Type': 'application/x-www-form-urlencoded'})

# Check if the request was successful
if response.status_code == 200:
    tokens = response.json()
    # print("Access Token:", tokens['access_token'])
    
    print("PASTE THIS IN THE .ENV FILE:")
    print("Refresh Token:", tokens['refresh_token'])
else:
    print("Failed to retrieve tokens")
    print("Status Code:", response.status_code)
    print("Response:", response.text)
