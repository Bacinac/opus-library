<script lang="ts">
	import { t, type MessageKey } from '$lib/i18n';
	import {
		ArmedButton,
		Button,
		Latest,
		Tag,
		formatBytes,
		formatRuntime,
		request,
		toasts,
		withLang,
		hold
	} from '$lib/kit';
	import {
		MediaHead,
		videoStateMark,
		StateMark
	} from '$lib/opus';
	import Poster from '$lib/video/Poster.svelte';
	import ReleasePicker from '$lib/video/ReleasePicker.svelte';
	import { subtitleLangs } from '$lib/video/langs.svelte';

	type Sub = { lang: string; source: string; forced: boolean; auto: boolean };
	type File = {
		resolution: string;
		video_codec: string;
		container: string;
		size: number;
		audio_langs: string[];
		subtitles: Sub[];
	};
	type Movie = {
		id: number;
		title: string;
		original_title: string;
		year: number | null;
		overview: string;
		poster_url: string | null;
		backdrop_url: string | null;
		runtime_min: number | null;
		status: 'wanted' | 'downloading' | 'complete' | 'waiting_subtitles';
		replacing?: boolean;
		missing_subs: string[];
		present_subs: string[];
		files?: File[];
	};

	let movies = $state<Movie[]>([]);
	let loaded = $state(false);
	let detail = $state<Movie | null>(null);
	let picking = $state<Movie | null>(null);
	let offered = $state(false);
	const opening = new Latest();

	async function load() {
		const got = await request<Movie[]>(withLang('/api/video/movies'));
		if (!got) return;
		movies = got;
		loaded = true;
	}

	$effect(() => {
		load();
		subtitleLangs.load();
	});

	async function open(movie: Movie) {
		const got = await request<Movie>(withLang(`/api/video/movies/${movie.id}`), {}, { latest: opening });
		if (got) detail = got;
		offered = false;
	}

	async function remove(movie: Movie) {
		if (!(await request(`/api/video/movies/${movie.id}`, { method: 'DELETE' }))) return;
		toasts.success(t('movies.deleted'));
		detail = null;
		await load();
	}
</script>

{#if detail}
	{@const d = detail}
	<MediaHead
		title={d.title}
		subtitle={[d.year ?? '', d.original_title !== d.title ? d.original_title : '', formatRuntime(d.runtime_min)]
			.filter(Boolean)
			.join(' · ')}
		overview={d.overview || t('detail.noOverview')}
		poster={d.poster_url}
		backdrop={d.backdrop_url}
	>
		{#snippet back()}
			<div class="back"><Button size="small" onclick={() => (detail = null)}>{t('movies.back')}</Button></div>
		{/snippet}
		{#snippet under()}
			<div class="facts">
				{#if videoStateMark(d.status)}<StateMark {...videoStateMark(d.status)!} text={t(`video.status.${d.status}`)} />{/if}
				{#if d.replacing}<StateMark {...videoStateMark('downloading')!} text={t('video.status.replacing')} />{/if}
				<span class="label">{t('detail.subtitles')}</span>
				{#each d.present_subs as lang (lang)}<Tag tone="ok">{lang.toUpperCase()}</Tag>{/each}
				{#each d.missing_subs as lang (lang)}<Tag tone="warn">{lang.toUpperCase()} ✕</Tag>{/each}
				{#if d.present_subs.length === 0 && d.missing_subs.length === 0}
					<span class="muted">—</span>
				{/if}
			</div>
		{/snippet}
		{#snippet actions()}
			{#if d.status === 'wanted'}
				<Button tone="primary" onclick={() => (picking = d)}>{t('movies.grab')}</Button>
			{:else if offered && d.files?.length && !d.replacing}
				<Button onclick={() => (picking = d)}>{t('movies.replace')}</Button>
			{/if}
			<ArmedButton onconfirm={() => remove(d)}>{t('common.delete')}</ArmedButton>
		{/snippet}
	</MediaHead>

	{#each d.files ?? [] as f, i (i)}
		<div class="file" use:hold={d.replacing ? undefined : () => (offered = true)}>
			<Tag>{f.resolution || '—'}</Tag>
			<span class="fmeta">
				{#if f.video_codec}<span>{f.video_codec}</span>{/if}
				{#if f.container}<span class="muted">{f.container}</span>{/if}
				{#if f.size}<span class="muted">{formatBytes(f.size)}</span>{/if}
				{#if f.audio_langs.length}<span class="muted">🔊 {f.audio_langs.join(', ')}</span>{/if}
			</span>
			<span class="fsubs">
				{#each f.subtitles as s, j (j)}
					<Tag
						tone={subtitleLangs.wanted.includes(s.lang) ? 'ok' : 'quiet'}
						title={t(`detail.subsource.${s.source}` as MessageKey)}
					>
						{s.lang.toUpperCase()}{s.forced ? ' ⏵' : ''}
					</Tag>
				{/each}
			</span>
		</div>
	{/each}
{:else}
	{#if loaded && movies.length === 0}
		<p class="muted">{t('movies.empty')}</p>
	{/if}
	<div class="grid">
		{#each movies as movie (movie.id)}
			<Poster
				title={movie.title}
				poster={movie.poster_url}
				sub={[movie.year ?? '', movie.present_subs.map((l) => l.toUpperCase()).join(' · ')]
					.filter(Boolean)
					.join(' · ')}
				mark={videoStateMark(movie.status)}
				markText={t(`video.status.${movie.status}`)}
				onclick={() => open(movie)}
			/>
		{/each}
	</div>
{/if}

{#if picking}
	<ReleasePicker
		base={`/api/video/movies/${picking.id}`}
		title={`${picking.title}${picking.year ? ` (${picking.year})` : ''}`}
		onclose={() => {
			picking = null;
			offered = false;
		}}
		ongrabbed={load}
	/>
{/if}

<style>
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 1rem;
	}
	.back {
		margin-bottom: 0.4rem;
	}
	.facts {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.4rem;
		margin-bottom: 0.6rem;
	}
	.label {
		margin-left: 0.4rem;
		font-weight: 600;
		font-size: var(--fs-m);
	}
	.file {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.75rem;
		margin-top: 0.8rem;
		padding: 0.6rem 0.9rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
	}
	.fmeta {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem;
		font-size: var(--fs-m);
		align-items: center;
	}
	.fsubs {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
		margin-left: auto;
	}
</style>
