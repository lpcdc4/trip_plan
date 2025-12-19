# Streamlit Itinerary (web deploy)

This is the *same* Streamlit UI/behavior as your local app, packaged for deployment.

## Local run (conda env)
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Render (easy)
1. Push these files to a GitHub repo
2. Render → New → Web Service → connect repo
3. Environment: Docker
4. Deploy

## Deploy on Fly.io (also works)
Use `fly launch` then `fly deploy` (Dockerfile included).
