# Clipit website

The public page for Clipit, built with TanStack Start (server-rendered React) and deployed to Cloudflare Workers.

```bash
npm ci
npm run dev       # http://localhost:3000
npm run build
npm run deploy    # build, then wrangler deploy
```

Page copy lives in `src/content.ts`, styles in `src/styles.css` and `src/styles/home.css`.

## Share image

`public/og.png` is a screenshot of the `/og` route. After changing it, with `npm run dev` running:

```bash
msedge --headless=new --hide-scrollbars --force-device-scale-factor=1 --window-size=1200,630 --virtual-time-budget=4000 --screenshot=public/og.png http://localhost:3000/og
```

`SITE_URL` in `src/content.ts` is the live address; change it if the site moves to a custom domain.
