// Walk a built OPUS demo in a real browser.
//
//   check  every page a visitor can reach, from each module's own routes and the
//          links on them, and fail on anything the synthetic API cannot answer,
//          a picture that is not there, a page error or a request leaving the demo
//   shots  the README screenshots, the animated tour and the social card, from
//          the same pages
//
// Run by deploy/demo.sh inside the Playwright image of the same version as the
// playwright-core in package-lock.json, with the packages installed under
// DEMO_WALK_PREFIX. Nothing leaves the demo but what the published site sends
// away too: the map tiles, to OpenStreetMap.
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const { chromium } = createRequire(`${process.env.DEMO_WALK_PREFIX}/`)('playwright-core');

const TYPES = {
	'.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
	'.json': 'application/json', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.png': 'image/png',
	'.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.webmanifest': 'application/manifest+json',
	'.mp4': 'video/mp4', '.webm': 'video/webm', '.vtt': 'text/vtt', '.mp3': 'audio/mpeg', '.flac': 'audio/flac',
	'.ogg': 'audio/ogg', '.apk': 'application/vnd.android.package-archive', '.txt': 'text/plain'
};

// As Cloudflare Pages answers: the site's _redirects first (a 200 rewrites, a 30x
// sends the browser on), then the file, the file with .html, the folder's index.
function rules(root) {
	let text = '';
	try {
		text = fs.readFileSync(path.join(root, '_redirects'), 'utf8');
	} catch {
		/* none */
	}
	return text.split('\n').map((l) => l.trim().split(/\s+/)).filter((r) => r.length >= 2 && !r[0].startsWith('#')).map(([from, to, status = '302']) => ({
		from: new RegExp(`^${from.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '(.*)')}$`),
		to,
		status: Number(status)
	}));
}

function serve(root) {
	const redirects = rules(root);
	const file = (p) => {
		for (const c of [p, `${p}.html`, path.join(p, 'index.html')]) {
			try {
				if (fs.statSync(c).isFile()) return c;
			} catch {
				/* next */
			}
		}
		return null;
	};
	const local = (pathname) => {
		const at = path.join(root, path.normalize(decodeURIComponent(pathname)));
		return at.startsWith(root) ? file(at) : null;
	};
	const server = http.createServer((req, res) => {
		const { pathname } = new URL(req.url, 'http://x');
		let found = local(pathname);
		let fallback = false;
		if (!found) {
			const rule = redirects.find((r) => r.from.test(pathname));
			const to = rule && rule.to.replace(':splat', pathname.match(rule.from)[1] ?? '');
			if (rule && rule.status !== 200) {
				res.writeHead(rule.status, { Location: to });
				res.end();
				return;
			}
			found = rule ? local(to) : null;
			fallback = !rule || to.endsWith('/index.html');
		}
		const sent = found ?? path.join(root, 'index.html');
		res.writeHead(200, {
			'Content-Type': TYPES[path.extname(sent)] ?? 'application/octet-stream',
			...(fallback ? { 'X-Demo-Fallback': '1' } : {})
		});
		fs.createReadStream(sent).pipe(res);
	});
	server.away = new Set(redirects.filter((r) => /^https?:/.test(r.to)).map((r) => new URL(r.to.replace(':splat', '')).origin));
	return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// Records every answer of 400 or more the synthetic API gives, before the demo replaces fetch.
const WATCH = () => {
	let inner = window.fetch;
	const wrap = (f) => async function (input, init) {
		const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
		const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
		const r = await f.call(this, input, init);
		if (r.status >= 400) {
			const body = await r.clone().text().catch(() => '');
			const sent = typeof init?.body === 'string' ? ` ← ${init.body.slice(0, 400)}` : '';
			console.warn(`demo-answer ${JSON.stringify({ method, url, status: r.status, body: body.slice(0, 160) + sent })}`);
		}
		return r;
	};
	Object.defineProperty(window, 'fetch', { configurable: true, get: () => inner, set: (v) => { inner = wrap(v); } });
};

const sites = Object.fromEntries(process.argv.slice(3).map((a) => {
	const [name, dir, routes, url] = a.split('=');
	return [name, { dir: path.resolve(dir), routes: path.resolve(routes), url }];
}));

function seeds(routes) {
	const out = [];
	const walk = (dir, at) => {
		for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
			if (!e.isDirectory()) continue;
			if (e.name.includes('[')) continue;
			const seg = /^\(.*\)$/.test(e.name) ? at : `${at}/${e.name}`;
			const here = path.join(dir, e.name);
			if (fs.existsSync(path.join(here, '+page.svelte'))) out.push(seg || '/');
			walk(here, seg);
		}
	};
	if (fs.existsSync(path.join(routes, '+page.svelte'))) out.push('/');
	walk(routes, '');
	return [...new Set(out)].sort();
}

// /movies/12 and /movies/31 are one page to a demo; three of each is enough.
const shape = (u) => u.pathname.replace(/\/\d+(?=\/|$)/g, '/#') + '?' +
	[...u.searchParams].map(([k, v]) => `${k}=${/^\d+$/.test(v) ? '#' : v}`).sort().join('&');

// What only a press reaches: a card opens a sheet rather than a page, and a film,
// a song or a quiz asks the API only once it is started. A step is a selector to
// press, a button's exact name, or a pause.
const ACTS = {
	player: [
		['/movies?find=all', ['button.card', { button: 'Play' }, { wait: 2500 }, { button: 'Details' }, { wait: 800 }]],
		['/series?find=all', ['button.card', { wait: 800 }, '.episodes button.what', { wait: 2000 }]],
		['/music?find=all', ['button.card', { wait: 800 }, 'button.record:not(.faint)', { wait: 800 }, 'button.play', { wait: 1500 }]],
		['/photos?find=places', ['ul.wheres button', { wait: 1500 }]],
		['/photos?find=guess', [{ button: 'Play' }, { wait: 2500 }]]
	]
};

async function act(page, steps) {
	for (const step of steps) {
		if (step.wait) {
			await page.waitForTimeout(step.wait);
			continue;
		}
		if (step.fill) {
			await page.locator(step.fill).first().fill(step.with);
			await page.keyboard.press('Enter');
			await page.waitForTimeout(400);
			continue;
		}
		const target = step.button
			? page.getByRole('button', { name: step.button, exact: true }).or(page.getByRole('tab', { name: step.button, exact: true }))
			: page.locator(step);
		await target.first().click({ timeout: 4000 });
		await page.waitForTimeout(400);
	}
}

async function open(browser, site, { tv = false, viewport = { width: 1440, height: 900 }, scale = 1 } = {}) {
	const server = await serve(site.dir);
	const origin = `http://127.0.0.1:${server.address().port}`;
	const context = await browser.newContext({ viewport, deviceScaleFactor: scale, serviceWorkers: 'block' });
	await context.addInitScript(WATCH);
	if (tv) await context.addInitScript(() => localStorage.setItem('opus.player.surface', 'tv'));
	await context.route((u) => !u.href.startsWith(origin) && !/^(data|blob):/.test(u.protocol), (r) => r.abort());
	return { server, origin, context };
}

async function check(browser, name, site, surface) {
	const { server, origin, context } = await open(browser, site, { tv: surface === 'tv' });
	const page = await context.newPage();
	const found = new Map();
	const note = (kind, what, where) => {
		const key = `${kind} ${what}`;
		if (!found.has(key)) found.set(key, where);
	};
	let here = '/';
	page.on('console', (m) => {
		const text = m.text();
		if (text.startsWith('demo-answer ')) {
			const a = JSON.parse(text.slice(12));
			note(`${a.status}`, `${a.method} ${new URL(a.url, origin).pathname}${new URL(a.url, origin).search}  ${a.body}`, here);
		} else if (m.type() === 'error' && !/Failed to load resource/.test(text)) note('console', text.slice(0, 200), here);
	});
	page.on('pageerror', (e) => note('error', String(e.message).slice(0, 200), here));
	page.on('requestfailed', (r) => {
		const to = new URL(r.url());
		if (to.origin !== origin && !server.away.has(to.origin)) note('outside', r.url().slice(0, 160), here);
	});
	page.on('response', async (r) => {
		const type = r.request().resourceType();
		if (!['image', 'media', 'font'].includes(type) || !r.url().startsWith(origin)) return;
		if (r.status() >= 400 || r.headers()['x-demo-fallback']) note('missing', new URL(r.url()).pathname, here);
	});

	const queue = seeds(site.routes).map((p) => surface === 'tv' ? `${p}?surface=tv` : p);
	const seen = new Set();
	const shapes = new Map();
	let pages = 0;
	while (queue.length && pages < 400) {
		const u = new URL(queue.shift(), origin);
		u.hash = '';
		if (seen.has(u.href)) continue;
		seen.add(u.href);
		const s = shape(u);
		if ((shapes.get(s) ?? 0) >= 3) continue;
		shapes.set(s, (shapes.get(s) ?? 0) + 1);
		here = u.pathname + u.search;
		pages++;
		if (process.env.DEMO_WALK_VERBOSE) console.log(`  · ${here}`);
		try {
			await page.goto(u.href, { waitUntil: 'load', timeout: 20000 });
			await page.waitForTimeout(900);
		} catch (e) {
			note('error', `navigation: ${e.message.split('\n')[0]}`, here);
			continue;
		}
		const landed = new URL(page.url());
		if (landed.pathname !== u.pathname && u.pathname !== '/') note('moved', `${u.pathname} → ${landed.pathname}`, here);
		const links = await page.$$eval('a[href]', (as) => as.map((a) => a.href));
		for (const l of links) {
			const v = new URL(l);
			if (v.origin !== origin || /^\/api\/|\.(apk|json|html)$/.test(v.pathname)) continue;
			v.hash = '';
			if (!seen.has(v.href)) queue.push(v.pathname + v.search);
		}
	}
	for (const [at, steps] of surface === 'tv' ? [] : ACTS[name] ?? []) {
		here = `${at} (pressed)`;
		try {
			await page.goto(new URL(at, origin).href, { waitUntil: 'load', timeout: 20000 });
			await page.waitForTimeout(900);
			await act(page, steps);
		} catch (e) {
			note('act', `${at}: ${e.message.split('\n')[0]}`, here);
			if (process.env.DEMO_WALK_FAILED) await page.screenshot({ path: path.join(process.env.DEMO_WALK_FAILED, `${name}-${at.replace(/\W+/g, '-')}.png`) });
		}
	}
	await context.close();
	server.close();
	const label = surface === 'tv' ? `${name} (tv)` : name;
	console.log(`${label}: ${pages} pages, ${found.size} problems`);
	for (const [what, where] of found) console.log(`  ${what}\n      on ${where}`);
	return found.size;
}

const command = process.argv[2];
const browser = await chromium.launch();
let problems = 0;
if (command === 'check') {
	for (const [name, site] of Object.entries(sites)) {
		problems += await check(browser, name, site, 'desktop');
		if (name === 'player') problems += await check(browser, name, site, 'tv');
	}
} else if (command === 'shots') {
	const { shots } = await import('./shots.mjs');
	await shots({ browser, sites, open, act });
} else {
	console.error('usage: walk.mjs check|shots <name>=<site>=<routes>=<public url> ...');
	process.exit(2);
}
await browser.close();
if (problems) {
	console.error(`demo walk: ${problems} problems`);
	process.exit(1);
}
