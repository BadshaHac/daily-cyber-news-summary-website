# Daily Cyber News Summary Maker

This project now includes both:

- a CLI utility to generate a date-wise cyber news summary
- a Flask website that works locally and is deployable to Vercel

## Run the website locally

```powershell
pip install -r .\requirements.txt
python .\app.py
```

Then open:

```text
http://127.0.0.1:8000
```

Website improvements include:

- cleaner dashboard-style layout
- quick date buttons
- source selection checkboxes
- feed failure warnings
- search box on the results page

## Deploy to Vercel

This project is set up so you do not need to run `python app.py` manually after deployment.

According to Vercel's official Flask docs, exposing a Flask `app` instance in `app.py` is enough for deployment:

- Flask on Vercel: [https://vercel.com/docs/frameworks/backend/flask/](https://vercel.com/docs/frameworks/backend/flask/)
- Vercel deploy CLI: [https://vercel.com/docs/cli/deploy](https://vercel.com/docs/cli/deploy)

Steps:

```powershell
npm install -g vercel
vercel login
vercel
```

For production:

```powershell
vercel --prod
```

After that, opening your Vercel URL will run the app automatically and generate summaries from the browser UI.

## GitHub to Vercel flow

If you want this to run from a Vercel link without opening PowerShell every time, use this flow:

1. Create a new GitHub repository.
2. Upload these project files to that repository:

```text
app.py
cyber_news_summary.py
README.md
requirements.txt
vercel.json
.gitignore
```

3. Go to Vercel and choose `Add New Project`.
4. Import the GitHub repository.
5. Vercel will detect the Python app and deploy it.
6. Open the Vercel project URL and use the website directly.

Official references:

- GitHub new repository: [https://docs.github.com/en/get-started/start-your-journey/hello-world](https://docs.github.com/en/get-started/start-your-journey/hello-world)
- Vercel import existing project: [https://vercel.com/docs/getting-started-with-vercel/import](https://vercel.com/docs/getting-started-with-vercel/import)
- Vercel GitHub integration: [https://vercel.com/github](https://vercel.com/github)

## Usage

```powershell
python .\cyber_news_summary.py --date 2026-04-20 --timezone Asia/Calcutta
```

Optional arguments:

- `--output-dir`: folder for generated markdown files. Default: `output`
- `--max-items-per-source`: limit items kept from each feed. Default: `10`
- `--timezone`: timezone used for date-wise filtering. Example: `Asia/Calcutta`

## Output

The script creates files like:

```text
output/cyber-news-summary-2026-04-20.md
```

That makes the summary date-wise, so you can generate one file per day.
