// Which vocabularies this install speaks, asked once and shared by every page
// that draws a type strip.
//
// The answer comes from the backend rather than from a setting, because it is
// not a preference: a type is present when its library tree is really mounted.
// An install with only music therefore IS a music app — no film word anywhere
// in it — without anyone switching anything off.

import { untrack } from 'svelte';
import { keep, recall, request } from '$lib/kit';
import type { MediaType } from './types';

const REMEMBERED = 'opus.library.type';

class Install {
	types = $state<MediaType[]>([]);
	loaded = $state(false);

	async load() {
		if (untrack(() => this.loaded)) return;
		this.types = (await request<MediaType[]>('/api/types')) ?? [];
		this.loaded = true;
	}

	/** the type to open on: the last one used, if this install still has it */
	remembered(): MediaType | null {
		if (!this.types.length) return null;
		const stored = recall(REMEMBERED) as MediaType | null;
		return stored && this.types.includes(stored) ? stored : this.types[0];
	}

	remember(type: MediaType) {
		keep(REMEMBERED, type);
	}
}

export const install = new Install();
