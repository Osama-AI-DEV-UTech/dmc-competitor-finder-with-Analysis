# DMC Competitor Finder

Apna package ka full-page screenshot upload karo → system saari inclusions nikalta hai → poore internet par search karta hai →
sirf wohi competitors dikhata hai jo **same complete package** bech rahe hain (har component, multi-day, ek total price) →
Streamlit analytics + Excel export.

Ek hi process: `streamlit run streamlit_app.py`. Koi FastAPI/alag server nahi. Sirf OpenAI use hota hai (Anthropic nahi).



## Run (local, Windows/Mac/Linux)
```bash
python -m venv .venv && .venv\Scripts\activate      # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                               # Mac/Linux: cp .env.example .env
# .env me OPENAI_API_KEY aur APP_PASSWORD set karo
streamlit run streamlit_app.py
```
Browser me password daalo, screenshot upload karo, "Review" step me components check karo, search start karo.

## Tabs (UI)
📊 Analytics (price chart, price-vs-match scatter, seller types, component heatmap) · 🏷️ Competitors (har competitor ki price + "Missing / unproven" column) ·
🔗 Pages to check · 📦 Your package · 🪵 Log. Upar Export to Excel / Search again.
Kisi competitor ke paas aapke kuch items na hon tab bhi uski price dikhti hai (Partial level) aur "All whole packages incl. partial" range alag se milti hai.
Har package/destination par chalta hai (multi-city, jaise Beijing+Shanghai ya Bali+Kuala Lumpur, bhi).

## 🧩 Gap analysis (price sahi hai ya nahi)
Har competitor ke liye teen cheezein: **✅ jo aap dono dete ho**, **❌ jo aap dete ho par unke page par nahi dikhta**, **➕ jo wo dete hain par aap nahi** (har extra ke saath unke page ka quote).
Saath me: har component kitne competitors dikhate hain (chart), sab se common extras (median price ke saath), aur ek verdict:
"Your price looks HIGH / COMPETITIVE / IN LINE" — sirf un competitors se jo wo sab dikhate hain jo aap dete ho (like-for-like). Excel me "Gap Analysis" sheet + Closest/Partial sheets me "Extras" aur "Price vs content" columns.
"Not shown" ka matlab: unke page ne prove nahi kiya (shayad wo phir bhi dete ho).

## Results kaise dikhte hain
Koi bhi competitor bilkul "as it is" nahi milta, isliye system **sab se qareebi packages** rank karke comparison deta hai.

| Level | Rule |
|---|---|
| **Same** | Aapke package ka har component page par proven (quote ke saath), whole multi-day package, ek total price. |
| **Near** | Kam az kam 70% components proven (strictness se badalta hai), koi hard conflict nahi. Jo missing hai wo "Missing" column me. |
| **Partial** | Whole package + price, par kuch components missing/unproven ya alag nights/hotel class. List me dikhta hai, main stats me nahi. |
| **Rejected** | Single activity (jaise sirf desert safari), hotel/flight-only, alag destination, ya unreadable. Reason likha hota hai. |

Price comparison (min/median/max/rank, chart) me **Same + Near** aate hain (strict mode me sirf Same). Shart: whole-package price, page par verbatim mili,
per-person basis clear, koi "sold out/expired" warning nahi, price aapki price ke 0.35×–3× ke andar, flights bundled nahi.
Strictness: strict (sirf exact) / standard (default, ≥70%) / relaxed (≥50%).

Anti-hallucination: model ka "yes" tabhi manta hai jab quote page text me mile; nights/days/stars page par regex se verify hote hain.

## Search
OpenAI web search (country-wise markets, default IN, PK, AE, GB, SG) + DuckDuckGo, dono saath, results merge. Koi extra paid vendor nahi.
`SEARCH_COUNTRIES` me max 10 country codes; usse lambi list ignore ho jati hai (warna searches/cost bahut barh jate).
Listing pages se package links bhi follow hote hain. Depth: fast (~25 pages) / standard (~45 + links) / deep (~80 + links).

## Excel sheets
Summary · Closest Matches (Same + Near, Missing column) · Partial Matches · Gap Analysis · Rejected (reason) · Pages To Check Manually · Evidence (quotes) · Reference Package · Assumptions (FX, rules).
USD columns live formulas hain (blue cells editable).

## Password
`APP_PASSWORD` (ya `APP_PASSWORD_SHA256`) set karna zaroori hai, warna app chalne se mana kar deta hai. 5 galat try par 1 minute lock.
Sirf local test ke liye `APP_AUTH_DISABLED=true`.

## Streamlit Community Cloud par deploy
1. Code GitHub (private repo) par push karo (`.env`, `data/` commit mat karo).
2. share.streamlit.io → New app → main file `streamlit_app.py`.
3. App settings → Secrets me `.streamlit/secrets.toml.example` ka content paste karo (OPENAI_API_KEY, APP_PASSWORD).
4. Notes: Cloud ka filesystem temporary hai — restart par history (SQLite) reset ho sakti hai (Excel download kar lo). Page refresh par dobara login karna padta hai.
   Apne server/VPS par deploy karo to `DB_PATH` persistent folder me rakho.

## Cost (andaza)
gpt-4.1 par ~40 pages ≈ $1–1.5 per run. Regional OpenAI searches ≈ $0.025–0.03 per call; deep mode me zyada. Models/search ke rates OpenAI ke pricing page par check karo.

## Limits (honest)
- Prices wahi hain jo page par dikh rahi hain (usually "from" prices), aapke travel dates ki guarantee nahi.
- DMC net rates logins ke peeche hote hain — public search me nahi aate.
- JS-heavy/blocked pages Jina reader se try hote hain; phir bhi na khule to "Pages to check" me aate hain.

## Tests
`pip install -r requirements.txt && python -m pytest tests -q` (LibreOffice ho to Excel formulas independently recalculate hote hain).

## Docker (optional)
```bash
docker build -t dmc-finder . && docker run -p 8501:8501 --env-file .env -v dmc_data:/app/data dmc-finder
```

## Project layout
`streamlit_app.py` (UI) · `ui/auth.py` (login) · `app/core.py` (service layer) · `app/services/` (extract, search, fetch, verify, match, export) · `tests/`.
