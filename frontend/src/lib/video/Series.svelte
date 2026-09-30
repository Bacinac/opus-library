<script lang="ts">
	import { t, type MessageKey } from '$lib/i18n';
	import {
		ArmedButton,
		Button,
		Latest,
		formatDate,
		formatNumber,
		plural,
		request,
		json,
		toasts,
		withLang
	} from '$lib/kit';
	import {
		SeriesPage,
		episodeCode,
		type PageEpisode,
		type PageSeason
	} from '$lib/opus';
	import { aired, countsOf, localDay, marksOf, type EpisodeRow } from '$lib/video/episodes';
	import ReleasePicker from '$lib/video/ReleasePicker.svelte';

	type SeriesRow = {
		id: number;
		title: string;
		year: number | null;
		poster_url: string | null;
		status: string;
		episodes_total: number;
		episodes_complete: number;
	};
	type SeriesDetail = {
		id: number;
		title: string;
		year: number | null;
		overview: string;
		poster_url: string | null;
		backdrop_url: string | null;
		monitored: boolean;
		seasons: { number: number; episodes: EpisodeRow[] }[];
	};

	// what TMDB says about a series' run, in its own English, and what the
	// reader is told instead
	const RUN: Record<string, MessageKey> = {
		'Returning Series': 'series.run.returning',
		'In Production': 'series.run.production',
		Planned: 'series.run.planned',
		Pilot: 'series.run.pilot',
		Ended: 'series.run.ended',
		Canceled: 'series.run.canceled'
	};

	let list = $state<SeriesRow[]>([]);
	let loaded = $state(false);
	let detail = $state<SeriesDetail | null>(null);
	let pickingEp = $state<{ id: number; label: string } | null>(null);
	let season = $state<string | number | null>(null);
	const showing = new Latest();

	async function load() {
		const got = await request<SeriesRow[]>(withLang('/api/video/series'));
		if (!got) return;
		list = got;
		loaded = true;
	}

	$effect(() => {
		load();
	});

	async function show(id: number) {
		const got = await request<SeriesDetail>(withLang(`/api/video/series/${id}`), {}, { latest: showing });
		if (!got) return;
		// a series opened afresh opens on its first season; the same series read
		// again after a grab keeps the season being worked in
		if (detail?.id !== got.id) season = null;
		detail = got;
	}

	async function back() {
		detail = null;
		await load();
	}

	function pick(episodeId: number) {
		const found = detail?.seasons
			.flatMap((s) => s.episodes.map((e) => ({ season: s.number, episode: e })))
			.find(({ episode }) => episode.id === episodeId);
		if (!detail || !found) return;
		pickingEp = {
			id: episodeId,
			label: `${detail.title} ${episodeCode(found.season, found.episode.number)}`
		};
	}

	async function toggleMonitor() {
		if (!detail) return;
		const next = !detail.monitored;
		if (!(await request(`/api/video/series/${detail.id}`, json({ monitored: next }, 'PATCH')))) return;
		detail.monitored = next;
		toasts.success(next ? t('series.monitorOn') : t('series.monitorOff'));
	}

	// kept current, so a page left open past midnight moves with the date
	let today = $state(localDay());
	$effect(() => {
		const tick = setInterval(() => (today = localDay()), 60_000);
		return () => clearInterval(tick);
	});

	async function downloadSeason(number: number) {
		if (!detail) return;
		const said = await request<{ queued: number }>(
			`/api/video/series/${detail.id}/seasons/${number}/search`,
			{ method: 'POST' }
		);
		if (!said) return;
		if (said.queued > 0)
			toasts.success(
				t('series.seasonQueued', {
					episodes: plural(said.queued, 'series.episodeCount.one', 'series.episodeCount.few', 'series.episodeCount.many')
				})
			);
		else toasts.info(t('series.seasonNothing'));
	}

	let seasons = $derived<PageSeason[]>(
		(detail?.seasons ?? []).map((season) => ({
			key: season.number,
			title: t('series.season', { n: formatNumber(season.number) }),
			counts: countsOf(season.episodes, today),
			episodes: season.episodes.map((episode): PageEpisode => {
				const future = !aired(episode, today);
				return {
					key: episode.id,
					number: episode.number,
					title: episode.title,
					description: episode.overview,
					image: episode.still_url,
					runtime: episode.runtime_min,
					dim: future,
					note: future && episode.air_date ? formatDate(episode.air_date) : '',
					tags: marksOf(episode, future),
					onhold: episode.file && !episode.replacing ? () => (offered = episode.id) : undefined
				};
			})
		}))
	);
	let wantedIn = $derived(
		new Map(
			(detail?.seasons ?? []).map((season) => [
				season.number,
				season.episodes.filter((e) => e.status === 'wanted' && aired(e, today)).length
			])
		)
	);
	let grabbable = $derived(
		new Set(
			(detail?.seasons ?? [])
				.flatMap((season) => season.episodes)
				.filter((e) => (e.status === 'wanted' || e.status === 'ignored') && aired(e, today))
				.map((e) => e.id)
		)
	);
	let offered = $state<number | null>(null);
	let owned = $derived(
		(detail?.seasons ?? []).flatMap((s) => s.episodes).filter((e) => !!e.file).length
	);
	let known = $derived((detail?.seasons ?? []).reduce((n, s) => n + s.episodes.length, 0));

	async function remove(row: SeriesRow) {
		if (!(await request(`/api/video/series/${row.id}`, { method: 'DELETE' }))) return;
		toasts.success(t('series.deleted'));
		await load();
	}
</script>

{#if detail}
	<SeriesPage
		title={detail.title}
		subtitle={String(detail.year ?? '')}
		overview={detail.overview || t('detail.noOverview')}
		poster={detail.poster_url}
		backdrop={detail.backdrop_url}
		count={`${formatNumber(owned)}/${formatNumber(known)}`}
		{seasons}
		bind:season
	>
		{#snippet back()}
			<div class="back"><Button size="small" onclick={back}>{t('series.back')}</Button></div>
		{/snippet}
		{#snippet actions()}
			<Button tone={detail!.monitored ? 'accent' : 'quiet'} selected={detail!.monitored} onclick={toggleMonitor}>
				{detail!.monitored ? t('series.monitored') : t('series.monitor')}
			</Button>
		{/snippet}
		{#snippet seasonActions(season)}
			{@const short = wantedIn.get(Number(season.key)) ?? 0}
			{#if short > 0}
				<Button size="small" tone="accent" onclick={() => downloadSeason(Number(season.key))}>
					{t('series.downloadSeason', { n: formatNumber(short) })}
				</Button>
			{/if}
		{/snippet}
		{#snippet episodeActions(episode)}
			{#if grabbable.has(Number(episode.key))}
				<Button size="small" onclick={() => pick(Number(episode.key))}>{t('episode.grab')}</Button>
			{:else if offered === Number(episode.key)}
				<Button size="small" tone="quiet" onclick={() => pick(Number(episode.key))}>{t('episode.replace')}</Button>
			{/if}
		{/snippet}
	</SeriesPage>
{:else}
	{#if loaded && list.length === 0}
		<p class="muted">{t('series.empty')}</p>
	{/if}
	<ul class="series-list">
		{#each list as row (row.id)}
			<li>
				{#if row.poster_url}
					<img src={row.poster_url} alt={row.title} loading="lazy" />
				{:else}
					<div class="placeholder"></div>
				{/if}
				<button class="title" onclick={() => show(row.id)}>
					<strong>{row.title}</strong>
					<span class="muted">
						{[row.year ?? '', RUN[row.status] ? t(RUN[row.status]) : ''].filter(Boolean).join(' · ')}
					</span>
				</button>
				<span class="muted count">
					{t('series.episodes', {
						done: formatNumber(row.episodes_complete),
						total: formatNumber(row.episodes_total)
					})}
				</span>
				<ArmedButton onconfirm={() => remove(row)}>{t('common.delete')}</ArmedButton>
			</li>
		{/each}
	</ul>
{/if}

{#if pickingEp}
	<ReleasePicker
		base={`/api/video/episodes/${pickingEp.id}`}
		title={pickingEp.label}
		onclose={() => {
			pickingEp = null;
			offered = null;
		}}
		ongrabbed={() => detail && show(detail.id)}
	/>
{/if}

<style>
	.back {
		margin-bottom: 0.4rem;
	}
	.series-list {
		list-style: none;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.series-list li {
		display: flex;
		align-items: center;
		gap: 1rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 0.5rem 1rem 0.5rem 0.5rem;
	}
	.series-list img,
	.placeholder {
		width: 46px;
		height: 69px;
		border-radius: 6px;
		object-fit: cover;
		background: var(--surface-2);
	}
	.title {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.15rem;
		background: none;
		border: none;
		font: inherit;
		color: inherit;
		cursor: pointer;
		text-align: left;
	}
	.count {
		font-size: var(--fs-m);
	}
</style>
