// The subtitle languages the library is asked to hold, as the settings say —
// what a file's subtitle is measured against, rather than two languages
// written into a screen.

import { request } from '$lib/kit';

type Setting = { key: string; value: string };

class SubtitleLangs {
	wanted = $state<string[]>([]);
	#asked = false;

	async load() {
		if (this.#asked) return;
		this.#asked = true;
		const settings = await request<Setting[]>('/api/settings');
		const langs = settings?.find((s) => s.key === 'subtitle_langs')?.value ?? '';
		this.wanted = langs
			.split(',')
			.map((l) => l.trim())
			.filter(Boolean);
	}
}

export const subtitleLangs = new SubtitleLangs();
