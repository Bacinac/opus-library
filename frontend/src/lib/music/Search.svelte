<script lang="ts">
	import { t } from '$lib/i18n';
	import { Latest, SearchBox, Tag, formatNumber, json, plural, request, toasts } from '$lib/kit';
	import Found from '$lib/core/Found.svelte';
	import type { ArtistSearchResult } from '$lib/music/types';

	function fans(n: number): string {
		if (n < 1000) return plural(n, 'search.fans.one', 'search.fans.few', 'search.fans.many');
		return t('search.fans.many', {
			n: formatNumber(n, { notation: 'compact', maximumFractionDigits: 1 })
		});
	}

	let query = $state('');
	let results = $state<ArtistSearchResult[]>([]);
	let searching = $state(false);
	let searched = $state(false);
	const asking = new Latest();

	async function search() {
		if (!query.trim()) return;
		searching = true;
		const found = await request<ArtistSearchResult[]>(
			`/api/music/search/artists?q=${encodeURIComponent(query)}`,
			{},
			{ latest: asking }
		);
		searching = false;
		if (!found) return;
		results = found;
		searched = true;
	}

	async function add(result: ArtistSearchResult) {
		const said = await request<{ id: number; name?: string; releases?: number; already?: boolean }>(
			'/api/music/artists',
			json(
				result.deezer_id != null ? { deezer_id: result.deezer_id } : { spotify_id: result.spotify_id }
			)
		);
		if (!said) return;
		if (said.already) toasts.info(t('artist.already', { name: result.name }));
		else
			toasts.success(
				t('artist.added', { name: said.name ?? result.name, releases: formatNumber(said.releases ?? 0) })
			);
	}
</script>

<SearchBox
	bind:value={query}
	placeholder={t('search.placeholder')}
	action={searching ? t('search.searching') : t('search.button')}
	busy={searching}
	onsubmit={search}
/>

{#if searched && results.length === 0}
	<p class="muted">{t('common.noResults')}</p>
{/if}

<Found
	items={results}
	key={(result) => result.deezer_id ?? result.spotify_id}
	picture={(result) => ({ src: result.image_url, alt: result.name })}
	onadd={add}
>
	{#snippet info(result)}
		{#if result.link}
			<a class="name" href={result.link} target="_blank" rel="noopener noreferrer">
				<strong>{result.name}</strong>
			</a>
		{:else}
			<strong>{result.name}</strong>
		{/if}
		<span class="muted">
			{#if result.spotify_id}<Tag tone="quiet">Spotify</Tag>{/if}
			{#if result.nb_album != null}
				{plural(result.nb_album, 'search.albums.one', 'search.albums.few', 'search.albums.many')}
			{/if}
			{#if result.nb_fan != null}
				&nbsp;·&nbsp;{fans(result.nb_fan)}
			{/if}
		</span>
		{#if result.matched_track}
			<span class="matched">{t('search.matchedTrack', { title: result.matched_track })}</span>
		{/if}
		{#if result.top_tracks.length > 0}
			<span class="muted">
				{t('search.topTracks', { tracks: result.top_tracks.join(' · ') })}
			</span>
		{/if}
	{/snippet}
</Found>

<style>
	strong {
		font-size: var(--fs-l);
	}
	.name {
		color: inherit;
		text-decoration: none;
	}
	.name:hover {
		text-decoration: underline;
	}
	.matched {
		color: var(--accent);
	}
</style>
