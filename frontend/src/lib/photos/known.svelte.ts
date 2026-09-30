// Everybody the photographs have a name for, asked once and shared by the
// screens that name, date and look up people.

import { request } from '$lib/kit';

export type Known = {
	id: number;
	name: string;
	born_on: string | null;
	born_source: string | null;
	/** may be shown on the screensaver; always so for somebody with an account */
	family: boolean;
	roster: boolean;
	cover: number | null;
};

class KnownPeople {
	list = $state<Known[]>([]);
	byId = $derived(new Map(this.list.map((k) => [k.id, k])));
	#asked = false;

	async load(again = false) {
		if (this.#asked && !again) return;
		this.#asked = true;
		const got = await request<Known[]>('/api/photos/people');
		if (got) this.list = got;
	}

	amend(id: number, change: Partial<Known>) {
		this.list = this.list.map((k) => (k.id === id ? { ...k, ...change } : k));
	}
}

export const known = new KnownPeople();
