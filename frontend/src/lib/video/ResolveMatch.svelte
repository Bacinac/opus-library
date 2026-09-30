<script lang="ts">
	import { onMount } from 'svelte';
	import { t } from '$lib/i18n';
	import { Dialog, Latest, SearchBox, Tag, json, request, toasts } from '$lib/kit';

	type Result = {
		tmdb_id: number;
		media_type: 'movie' | 'tv';
		title: string;
		year: number | null;
		poster_url: string | null;
	};

	let {
		path,
		filename,
		guessed,
		onclose,
		onresolved
	}: {
		path: string;
		filename: string;
		guessed?: string;
		onclose: () => void;
		onresolved: () => void;
	} = $props();

	let query = $state('');
	let results = $state<Result[]>([]);
	let loading = $state(false);
	let searched = $state(false);
	let picking = $state(false);
	const films = new Latest();
	const shows = new Latest();

	async function search() {
		if (!query.trim()) return;
		loading = true;
		const q = encodeURIComponent(query);
		const [m, s] = await Promise.all([
			request<Result[]>(`/api/video/search/movies?q=${q}`, {}, { latest: films }),
			request<Result[]>(`/api/video/search/series?q=${q}`, {}, { latest: shows })
		]);
		loading = false;
		results = [
			...(m ?? []).map((x) => ({ ...x, media_type: 'movie' as const })),
			...(s ?? []).map((x) => ({ ...x, media_type: 'tv' as const }))
		];
		searched = true;
	}

	onMount(() => {
		if (guessed) {
			query = guessed;
			search();
		}
	});

	async function pick(r: Result) {
		if (picking) return;
		picking = true;
		const done = await request(
			'/api/video/library/resolve',
			json({ path, media_type: r.media_type, tmdb_id: r.tmdb_id })
		);
		picking = false;
		if (!done) return;
		toasts.success(t('review.resolved', { title: r.title }));
		onresolved();
		onclose();
	}
</script>

<Dialog title={t('review.matchTitle')} subtitle={filename} size="narrow" {onclose}>
	<SearchBox bind:value={query} placeholder={t('video.titleSearch')} action={t('common.search')} onsubmit={search} />
	{#if loading}
		<p class="note">{t('common.loading')}</p>
	{:else if searched && results.length === 0}
		<p class="note">{t('common.noResults')}</p>
	{:else}
		<ul>
			{#each results as r (r.media_type + r.tmdb_id)}
				<li>
					<button class="row" onclick={() => pick(r)} disabled={picking}>
						{#if r.poster_url}<img src={r.poster_url} alt={r.title} loading="lazy" />{:else}<div class="ph"></div>{/if}
						<span class="info">
							<strong>{r.title} <span class="muted">{r.year ?? ''}</span></strong>
							<Tag kind={r.media_type === 'movie' ? 'film' : 'series'}>
								{t(r.media_type === 'movie' ? 'review.movie' : 'review.series')}
							</Tag>
						</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</Dialog>

<style>
	.note {
		color: var(--muted);
		text-align: center;
		padding: 1.5rem;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.row {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 0.8rem;
		text-align: left;
		font: inherit;
		color: inherit;
		padding: 0.4rem 0.6rem;
		border-radius: 9px;
		border: 1px solid var(--border);
		background: var(--bg);
		cursor: pointer;
	}
	.row:hover:not(:disabled) {
		border-color: var(--accent);
		background: var(--surface-2);
	}
	.row:disabled {
		opacity: 0.6;
	}
	.row img,
	.row .ph {
		width: 42px;
		height: 63px;
		border-radius: 6px;
		object-fit: cover;
		background: var(--surface-2);
		flex: none;
	}
	.info {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.3rem;
		min-width: 0;
	}
</style>
