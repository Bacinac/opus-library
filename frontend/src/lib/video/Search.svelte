<script lang="ts">
	import { t } from '$lib/i18n';
	import { Latest, SearchBox, json, request, toasts } from '$lib/kit';
	import Found from '$lib/core/Found.svelte';

	type Title = {
		tmdb_id: number;
		title: string;
		original_title: string;
		year: number | null;
		poster_url: string | null;
		streaming?: { id: number; name: string; ours: boolean }[];
	};

	const WORDS = {
		movies: { placeholder: 'movies.titleSearch', added: 'movies.added', already: 'movies.already' },
		series: { placeholder: 'series.titleSearch', added: 'series.added', already: 'series.already' }
	} as const;

	let { kind }: { kind: keyof typeof WORDS } = $props();

	let query = $state('');
	let results = $state<Title[]>([]);
	let searching = $state(false);
	let searched = $state(false);
	const asking = new Latest();

	async function search() {
		if (!query.trim()) return;
		searching = true;
		const found = await request<Title[]>(
			`/api/video/search/${kind}?q=${encodeURIComponent(query)}`,
			{},
			{ latest: asking }
		);
		searching = false;
		if (!found) return;
		results = found;
		searched = true;
	}

	async function add(found: Title) {
		const said = await request(`/api/video/${kind}`, json({ tmdb_id: found.tmdb_id }), {
			on: { 409: () => toasts.info(t(WORDS[kind].already, { title: found.title })) }
		});
		if (said) toasts.success(t(WORDS[kind].added, { title: found.title }));
	}
</script>

<SearchBox
	bind:value={query}
	placeholder={t(WORDS[kind].placeholder)}
	action={searching ? t('search.searching') : t('search.button')}
	busy={searching}
	onsubmit={search}
/>

{#if searched && results.length === 0}
	<p class="muted">{t('common.noResults')}</p>
{/if}

<Found
	items={results}
	key={(found) => found.tmdb_id}
	picture={(found) => ({ src: found.poster_url, alt: found.title })}
	poster
	onadd={add}
>
	{#snippet info(found)}
		<strong>{found.title}</strong>
		<span class="muted">
			{[found.year ?? '', found.original_title !== found.title ? found.original_title : '']
				.filter(Boolean)
				.join(' · ')}
		</span>
		{#if found.streaming?.length}
			<span class="streams" class:ours={found.streaming.some((s) => s.ours)}>
				{t('video.streamingOn', { services: found.streaming.map((s) => s.name).join(', ') })}
			</span>
		{/if}
	{/snippet}
</Found>

<style>
	strong {
		font-size: var(--fs-l);
	}
	.streams {
		color: var(--muted);
	}
	.streams.ours {
		color: var(--ok);
	}
</style>
