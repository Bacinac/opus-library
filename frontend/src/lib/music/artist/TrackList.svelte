<script lang="ts">
	// The expanded release: its numbered tracklist with durations, the inline
	// title editor, the controls that reconcile a file tag with the catalog
	// title, and the files under the folder that matched no track. Every write
	// is the page's — this view only says which row it happened on.

	import { t } from '$lib/i18n';
	import { ArmedButton, Button, duration, formatNumber } from '$lib/kit';
	import { sameTitle } from '$lib/music/releases';
	import type { ReleaseTracks, TrackRow } from '$lib/music/types';

	let {
		releaseId,
		data,
		editingRow,
		editingTitle = $bindable(),
		onretagall,
		onfixtag,
		onstartedit,
		oncanceledit,
		onsavetitle,
		onaccepttag,
		ondeletefile
	}: {
		releaseId: number;
		data: ReleaseTracks;
		editingRow: TrackRow | null;
		editingTitle: string;
		onretagall: (releaseId: number) => void;
		onfixtag: (releaseId: number, row: TrackRow) => void;
		onstartedit: (row: TrackRow) => void;
		oncanceledit: () => void;
		onsavetitle: (row: TrackRow) => void;
		onaccepttag: (row: TrackRow) => void;
		ondeletefile: (releaseId: number, fileId: number) => void;
	} = $props();

	// which row has its file's tags open, one at a time
	let showTags = $state<number | null>(null);

	function focusSelect(node: HTMLInputElement) {
		node.focus();
		node.select();
	}

	const fileTitle = (row: TrackRow) =>
		row.file_name
			? [
					row.file_name,
					t('artist.tagSays', { value: `${row.tag_track ?? '—'}. ${row.tag_title ?? '—'}` }),
					`${row.tag_artist ?? '—'} — ${row.tag_album ?? '—'}`
				].join('\n')
			: '';
</script>

{#if data.description}
	<p class="album-desc">{data.description}</p>
{/if}
{#if data.tracks.some((r) => r.has_file)}
	<p class="retag-row">
		<Button size="small" title={t('artist.retagAllTitle')} onclick={() => onretagall(releaseId)}>
			{t('artist.retagAll')}
		</Button>
	</p>
{/if}
{#if data.tracks.length === 0}
	<p class="muted no-tracklist">{t('artist.noTracklist')}</p>
{/if}
<ol class="tracks">
	{#each data.tracks as row (row.id)}
		<li
			class:missing={!row.has_file && data.status !== 'complete'}
			class:bonus={!row.has_file && data.status === 'complete'}
			title={fileTitle(row)}
		>
			<span class="pos">{row.position}.</span>
			{#if editingRow?.id === row.id}
				<input
					class="title-edit"
					type="text"
					bind:value={editingTitle}
					use:focusSelect
					aria-label={t('artist.editTitle')}
					onkeydown={(e) => {
						if (e.key === 'Enter') onsavetitle(row);
						else if (e.key === 'Escape') oncanceledit();
					}}
					onblur={() => {
						if (editingTitle.trim() === row.title) oncanceledit();
					}}
				/>
				<Button size="small" label={t('common.save')} title={t('common.save')} onclick={() => onsavetitle(row)}>
					✓
				</Button>
			{:else}
				{row.title}
				<span class="edit-title">
					<Button size="small" label={t('artist.editTitle')} title={t('artist.editTitle')} onclick={() => onstartedit(row)}>
						✎
					</Button>
				</span>
			{/if}
			{#if row.tag_title && !sameTitle(row.tag_title, row.title)}
				<span class="tag-note">{t('artist.tagSays', { value: `„${row.tag_title}“` })}</span>
				<Button size="small" onclick={() => onfixtag(releaseId, row)}>{t('artist.fixTag')}</Button>
				<Button size="small" title={t('artist.acceptTagTitle')} onclick={() => onaccepttag(row)}>
					{t('artist.acceptTag')}
				</Button>
			{/if}
			{#if !row.has_file}
				{#if data.status === 'complete'}
					<span class="muted note"> — {t('artist.trackBonus')}</span>
				{:else}
					<span class="note"> — {t('artist.trackMissing')}</span>
				{/if}
			{/if}
			{#if row.duration_sec}
				<span class="muted track-dur">{duration(row.duration_sec)}</span>
			{/if}
			<!-- Everything the file itself carries, which until now could only be
			     read by opening it. Shut by default: a row is a song, and
			     forty-one fields of what its file says is the answer to a
			     question somebody has to ask first. -->
			{#if row.tags && Object.keys(row.tags).length}
				<Button size="small" selected={showTags === row.id}
					onclick={() => (showTags = showTags === row.id ? null : row.id)}>
					{t('artist.fileTags', { n: formatNumber(Object.keys(row.tags).length) })}
				</Button>
				{#if showTags === row.id}
					<dl class="file-tags">
						{#each Object.entries(row.tags).sort(([a], [b]) => a.localeCompare(b)) as [key, value] (key)}
							<dt>{key}</dt>
							<dd>{Array.isArray(value) ? value.join(' · ') : value}</dd>
						{/each}
					</dl>
				{/if}
			{/if}
		</li>
	{/each}
</ol>
{#if data.links.length > 0}
	<div class="meta-line release-links">
		<span class="meta-label">{t('artist.ids')}</span>
		{#each data.links as link (link.source)}
			<a class="meta-link" href={link.url} target="_blank" rel="noreferrer">
				{link.source}
			</a>
		{/each}
	</div>
{/if}
{#if data.unmatched_files.length > 0}
	<p class="unmatched-title">{t('artist.unmatchedFiles')}:</p>
	<ul class="unmatched-files">
		{#each data.unmatched_files as file (file.id)}
			<li>
				<code>{file.name}</code>
				<ArmedButton size="small" label={t('artist.deleteFileTitle')} title={t('artist.deleteFileTitle')}
					onconfirm={() => ondeletefile(releaseId, file.id)}>✕</ArmedButton>
			</li>
		{/each}
	</ul>
{/if}

<style>
	.meta-line {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		column-gap: 0.9rem;
		row-gap: 0.15rem;
		margin-top: 0.75rem;
	}
	.meta-label {
		font-size: var(--fs-xs);
		font-weight: 700;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--muted);
		min-width: 5.5rem;
	}
	.meta-link {
		font-size: var(--fs-m);
		color: var(--muted);
		text-decoration: none;
	}
	.meta-link:hover {
		color: var(--accent);
		text-decoration: underline;
	}
	.tracks {
		margin: 0.25rem 0;
		padding-left: 1.5rem;
		columns: 2;
		column-gap: 3rem;
		font-size: var(--fs-m);
		list-style: none;
	}
	.tracks li {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.35rem;
		padding: 0.15rem 0;
		break-inside: avoid;
	}
	.pos {
		display: inline-block;
		min-width: 1.8em;
		color: var(--muted);
	}
	.track-dur {
		margin-left: auto;
		padding-left: 0.75rem;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.tracks li.missing {
		color: var(--danger);
	}
	.tracks li.bonus {
		color: var(--muted);
	}
	.note {
		font-size: 0.85em;
	}
	.release-links {
		margin: 0.35rem 0 0.5rem;
	}
	/* two columns, the key against the value, because forty of these read as a
	   wall of text in any other shape */
	.file-tags {
		display: grid;
		grid-template-columns: minmax(9rem, max-content) 1fr;
		gap: 0.1rem 0.8rem;
		width: 100%;
		margin: 0.4rem 0 0.6rem 1.8rem;
		font-size: var(--fs-s);
	}
	.file-tags dt {
		color: var(--muted);
		font-family: ui-monospace, monospace;
	}
	.file-tags dd {
		margin: 0;
		overflow-wrap: anywhere;
		/* a lyric sheet in a tag must not push the list off the screen */
		max-height: 4.5rem;
		overflow: auto;
	}
	.tag-note {
		color: var(--warn);
		font-size: 0.82em;
	}
	.edit-title {
		opacity: 0;
	}
	.tracks li:hover .edit-title,
	.edit-title:focus-within {
		opacity: 1;
	}
	.title-edit {
		width: min(22rem, 60%);
		padding: 0.05rem 0.4rem;
	}
	.retag-row {
		margin: 0.3rem 0 0.5rem;
	}
	.unmatched-title {
		margin: 0.5rem 0 0.25rem;
		color: var(--warn);
		font-size: var(--fs-m);
		font-weight: 600;
	}
	.unmatched-files {
		margin: 0 0 0.5rem;
		padding-left: 1.5rem;
		font-size: var(--fs-m);
		color: var(--muted);
	}
	.unmatched-files li {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.no-tracklist {
		margin: 0.4rem 0;
		font-size: var(--fs-m);
	}
	.album-desc {
		margin: 0.5rem 0 0.75rem;
		font-size: var(--fs-m);
		line-height: 1.55;
		color: var(--muted);
		white-space: pre-line;
	}
</style>
