# Fusion Backend is a backend system similar to playfab that FRAGMENT will be using, this is self hosted (up 24/7) so i decided to release it for free
## [Fusion Backend Dashboard](https://fusionstudiosvr.com/Dash/)
# 1. Deploy the backend to Render

1. Push the backend folder to a new GitHub repo.
2. In Render log in or create a new account then press: New → Web Service, link your github account and open the backend repo.
3. Settings:
   - Build command: **pip install -r requirements.txt**
   - Start command: **gunicorn app:app**
4. Add an environment variable:
   - **ADMIN_KEY** = a random secret string (like a password for the dashboard). Generate one by opening your cmd and pasting:
     ```
     python -c "import secrets; print(secrets.token_hex(32))"
     ```
     or simply write random numbers and letters.
     go to Environment variables and paste **ADMIN_KEY** in the Key tab and paste your key in the Value tab
5. Deploy to Render and it'll give you a URL like `https://backend.onrender.com`.
6. log in or create an account at `uptimerobot.com`, then choose HTTP / website monitoring and paste your health render url (`https://backend.onrender.com/health`) and make the monitor interval 5 minutes

## 2. Import the SDK to unity

1. Download the `Fusion SDK` unity package.
2. Import the Backend Manager Prefab (located in FusionSDK➡Examples➡Prefabs)
3. Go to the API Client child object of Backend Manager and paste in your backend url `https://backend.onrender.com`.
4. Now you can view the Bootstrapper child object of Backend Manager, and add the currency code you created.
5. Then you can go to FusionSDK➡Examples➡Prefabs to test the Shop Example, Cloud Script Example, and Ban Example.
