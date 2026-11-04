// The pictures each module's README shows, taken from the demo just walked:
// four screens as stills, the same four as an animated tour, and the card a
// link to the repository unfolds into. Written into <module>/docs/screenshots.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const need = createRequire(`${process.env.DEMO_WALK_PREFIX}/`);
const sharp = need('sharp');

// A still is 1920x1200: a 1440x900 desktop at 4/3, or the 960-wide page a
// television box actually draws, at the box's own 2.
const DESK = { viewport: { width: 1440, height: 900 }, scale: 4 / 3 };
const TV = { tv: true, viewport: { width: 960, height: 600 }, scale: 2 };

const FRAMES = {
	player: [
		{ file: 'home', at: '/' },
		{ file: 'music', at: '/music' },
		{ file: 'photos', at: '/photos?find=people' },
		{ file: 'tv', at: '/', ...TV }
	],
	library: [
		{ file: 'music', at: '/', steps: [{ button: 'Music' }] },
		{ file: 'artist', at: '/artists/1' },
		{ file: 'films', at: '/', steps: [{ button: 'Films' }] },
		{ file: 'people', at: '/', steps: [{ button: 'Photo' }, { button: 'People' }] }
	],
	downloads: [
		{ file: 'jobs', at: '/jobs' },
		{ file: 'search', at: '/search', steps: [{ fill: 'input', with: 'Agent 327' }, { wait: 1200 }] },
		{ file: 'services', at: '/services' },
		{ file: 'settings', at: '/settings' }
	]
};

const CARD = {
	player: {
		shot: 'tv',
		title: 'One frontend for every screen in the house',
		lines: ['Television, phone, car and DAC', 'Films, series, records and the family album']
	},
	library: {
		shot: 'music',
		title: 'One library for music, films, series and photographs',
		lines: ['Artists and series followed · releases judged', 'Faces through the years · places on a map']
	},
	downloads: {
		shot: 'jobs',
		title: 'One interface over every source of acquisition',
		lines: ['Usenet, torrents, Soulseek and web video', 'One search · one queue · behind a VPN']
	}
};

// The interface names the system's own sans-serif, so each machine draws it in
// a different face. The pictures use Roboto, the face every Android phone and
// television draws it in, carried in rather than installed into the image.
function roboto() {
	const dir = path.dirname(need.resolve('@fontsource-variable/roboto/index.css'));
	return fs.readFileSync(path.join(dir, 'index.css'), 'utf8')
		.replace(/'Roboto Variable'/g, "'Roboto'")
		.replace(/url\(\.\/files\/([^)]+)\)/g, (_, f) => `url(data:font/woff2;base64,${fs.readFileSync(path.join(dir, 'files', f)).toString('base64')})`);
}

// Ready is when the fonts are in, the pictures in view have arrived and the
// page has stopped moving; a picture further down never arrives on its own.
async function settle(page) {
	await page.waitForLoadState('networkidle').catch(() => {});
	await page.evaluate(() => Promise.race([
		new Promise((r) => setTimeout(r, 8000)),
		document.fonts.ready.then(() => Promise.all([...document.images]
			.filter((i) => !i.complete && i.getBoundingClientRect().top < innerHeight)
			.map((i) => new Promise((r) => {
				i.addEventListener('load', r);
				i.addEventListener('error', r);
			}))))
	]));
	await page.waitForTimeout(1200);
}

async function still(browser, site, frame, open, act, font) {
	const { server, origin, context } = await open(browser, site, frame.tv ? TV : DESK);
	try {
		const page = await context.newPage();
		await page.goto(new URL(frame.at, origin).href, { waitUntil: 'load', timeout: 20000 });
		await page.addStyleTag({ content: font });
		await settle(page);
		await act(page, frame.steps ?? []);
		await settle(page);
		return await page.screenshot({ type: 'png' });
	} finally {
		await context.close();
		server.close();
	}
}

async function tour(stills, to) {
	const frames = await Promise.all(stills.map((s) => sharp(s).resize(1280).png().toBuffer()));
	await sharp(frames, { join: { animated: true } })
		.webp({ loop: 0, delay: frames.map(() => 2500), quality: 75, effort: 6 })
		.toFile(to);
}

const escape = (s) => s.replace(/[&<>"]/g, (c) => `&${{ '&': 'amp', '<': 'lt', '>': 'gt', '"': 'quot' }[c]};`);

async function card(browser, font, { mark, url, said, shot }, to) {
	const page = await browser.newPage({ viewport: { width: 1280, height: 640 } });
	try {
		await page.setContent(`<!doctype html><html><head><style>${font}
			html, body { margin: 0; width: 1280px; height: 640px; overflow: hidden; }
			body { background: radial-gradient(1100px 640px at 110% 120%, #133a38 0%, #0e1213 60%); color: #e9eeee; font-family: 'Roboto', sans-serif; }
			.say { position: absolute; left: 72px; top: 96px; width: 520px; }
			.say img { height: 70px; display: block; }
			h1 { margin: 44px 0 0; font-size: 42px; line-height: 1.16; font-weight: 700; letter-spacing: -0.01em; }
			p { margin: 30px 0 0; font-size: 21px; line-height: 1.5; color: #9aa7a9; }
			.url { margin-top: 30px; font-size: 19px; font-weight: 700; color: #35b3ad; }
			.shot { position: absolute; left: 650px; top: 70px; width: 960px; height: 600px; border-radius: 16px; overflow: hidden;
				border: 1px solid rgba(255, 255, 255, 0.1); box-shadow: 0 30px 80px rgba(0, 0, 0, 0.55); }
			.shot img { width: 100%; height: 100%; display: block; }
		</style></head><body>
			<div class="say">
				<img src="data:image/svg+xml;base64,${fs.readFileSync(mark).toString('base64')}" alt="">
				<h1>${escape(said.title)}</h1>
				<p>${said.lines.map(escape).join('<br>')}</p>
				<div class="url">${escape(new URL(url).host)}</div>
			</div>
			<div class="shot"><img src="data:image/png;base64,${shot.toString('base64')}" alt=""></div>
		</body></html>`);
		await page.evaluate(() => document.fonts.ready);
		await page.screenshot({ path: to, type: 'png' });
	} finally {
		await page.close();
	}
}

export async function shots({ browser, sites, open, act }) {
	const font = roboto();
	for (const [name, site] of Object.entries(sites)) {
		const repo = path.resolve(site.routes, '../../..');
		const out = path.join(repo, 'docs/screenshots');
		const stills = {};
		for (const frame of FRAMES[name]) {
			stills[frame.file] = await still(browser, site, frame, open, act, font);
			await sharp(stills[frame.file]).webp({ quality: 82, effort: 6 }).toFile(path.join(out, `${frame.file}.webp`));
		}
		await tour(Object.values(stills), path.join(out, 'tour.webp'));
		await card(browser, font, {
			mark: path.join(repo, `frontend/src/lib/opus/marks/opus-${name}.svg`),
			url: site.url,
			said: CARD[name],
			shot: stills[CARD[name].shot]
		}, path.join(out, 'social-preview.png'));
		console.log(`${name}: ${FRAMES[name].length} stills, the tour and the card -> ${out}`);
	}
}
