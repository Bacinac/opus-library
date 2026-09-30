<script lang="ts">
	// The artist page shell: it loads the artist, polls while a release is in
	// flight, and owns every write the views below it can trigger together with
	// the state those writes read. The header, the identity panel, the tables
	// and the tracklist only draw and report back.

	import { page } from '$app/state';
	import { t } from '$lib/i18n';
	import {
		Button,
		Latest,
		Notice,
		formatNumber,
		json,
		plural,
		request,
		toasts
	} from '$lib/kit';
	import ArtworkPicker from '$lib/music/ArtworkPicker.svelte';
	import ArtistHeader from '$lib/music/artist/ArtistHeader.svelte';
	import CandidatePicker from '$lib/music/artist/CandidatePicker.svelte';
	import IdentityPanel from '$lib/music/artist/IdentityPanel.svelte';
	import ReleaseTable from '$lib/music/artist/ReleaseTable.svelte';
	import TrackList from '$lib/music/artist/TrackList.svelte';
	import { isMain, mainShelf } from '$lib/music/releases';
	import { BUSY } from '$lib/music/status';
	import type { ArtistDetail, Release, ReleaseTracks, TrackRow } from '$lib/music/types';

	let artist = $state<ArtistDetail | null>(null);
	let picker = $state<{ entity: 'artists' | 'releases'; id: number; title: string } | null>(null);
	let candidatePicking = $state<Release | null>(null);

	let expandedReleaseId = $state<number | null>(null);
	let trackLists = $state<Record<number, ReleaseTracks>>({});
	let editingRow = $state<TrackRow | null>(null);
	let editingTitle = $state('');

	const asking = new Latest();

	async function load() {
		const got = await request<ArtistDetail>(`/api/music/artists/${page.params.id}`, {}, { latest: asking });
		if (got) artist = got;
	}

	// a member's or a group's link opens the same page with another id, and
	// nothing of the artist before it may survive onto it
	$effect(() => {
		void page.params.id;
		artist = null;
		expandedReleaseId = null;
		trackLists = {};
		editingRow = null;
		picker = null;
		load();
	});

	async function loadTracks(releaseId: number) {
		const got = await request<ReleaseTracks>(`/api/music/releases/${releaseId}/tracks`);
		if (got) trackLists[releaseId] = got;
	}

	async function openTracks(releaseId: number) {
		const url = `/api/music/releases/${releaseId}/tracks`;
		const got = await request<ReleaseTracks>(url);
		if (!got) return;
		trackLists[releaseId] = got;
		if (got.tracks.length && got.description !== null) return;
		const filled = await request<ReleaseTracks>(url, { method: 'POST' });
		if (filled) trackLists[releaseId] = filled;
	}

	async function toggleTracks(release: Release) {
		if (expandedReleaseId === release.id) {
			expandedReleaseId = null;
			return;
		}
		if (!trackLists[release.id]) await openTracks(release.id);
		expandedReleaseId = release.id;
	}

	async function retagAll(releaseId: number) {
		const data = await request<{ files: number }>(`/api/music/releases/${releaseId}/retag`, {
			method: 'POST'
		});
		if (!data) return;
		toasts.success(
			t('artist.retagDone', {
				files: plural(data.files, 'music.library.files.one', 'music.library.files.few', 'music.library.files.many')
			})
		);
		await loadTracks(releaseId);
	}

	async function fixTag(releaseId: number, row: TrackRow) {
		if (!(await request(`/api/music/tracks/${row.id}/fix-tag`, { method: 'POST' }))) return;
		toasts.success(t('artist.tagFixed'));
		await loadTracks(releaseId);
	}

	async function startEditTitle(row: TrackRow) {
		if (editingRow && editingRow !== row) await saveEditedTitle(editingRow);
		editingRow = row;
		editingTitle = row.title;
	}

	async function patchTitle(row: TrackRow, title: string): Promise<boolean> {
		const data = await request<{ title: string }>(`/api/music/tracks/${row.id}`, json({ title }, 'PATCH'));
		if (!data) return false;
		row.title = data.title;
		toasts.success(t('artist.titleSaved'));
		return true;
	}

	async function saveEditedTitle(row: TrackRow) {
		const title = editingTitle.trim();
		if (!title || title === row.title) {
			editingRow = null;
			return;
		}
		if (await patchTitle(row, title)) editingRow = null;
	}

	async function refreshReleases() {
		const shown = artist;
		const fresh = await request<ArtistDetail>(`/api/music/artists/${page.params.id}`, {}, { latest: asking });
		if (!fresh || !shown || fresh.id !== shown.id) return;
		for (const r of fresh.releases) {
			const old = shown.releases.find((o) => o.id === r.id);
			if (old && old.status !== r.status && !BUSY.includes(r.status)) {
				if (expandedReleaseId === r.id) await loadTracks(r.id);
				else delete trackLists[r.id];
			}
		}
		artist = fresh;
	}

	$effect(() => {
		if (!artist?.releases.some((r) => BUSY.includes(r.status))) return;
		const interval = setInterval(refreshReleases, 4000);
		return () => clearInterval(interval);
	});

	let studioReleases = $derived(mainShelf(artist?.releases ?? []));
	let otherReleases = $derived(artist?.releases.filter((r) => !isMain(r)) ?? []);

	async function download(release: Release, mode: 'full' | 'replace' | 'surround' | 'dsd' = 'full') {
		if (!(await request(`/api/music/releases/${release.id}/download`, json({ mode })))) return;
		toasts.success(t('artist.downloadStarted', { title: release.title }));
		await load();
	}

	async function deleteFiles(release: Release) {
		if (!(await request(`/api/music/releases/${release.id}/files`, { method: 'DELETE' }))) return;
		toasts.success(t('artist.filesDeleted', { title: release.title }));
		await load();
	}

	async function deleteFile(releaseId: number, fileId: number) {
		if (!(await request(`/api/music/files/${fileId}`, { method: 'DELETE' }))) return;
		toasts.success(t('artist.fileDeleted'));
		await loadTracks(releaseId);
		await load();
	}

	async function refreshMetadata() {
		if (!artist) return;
		if (await request(`/api/music/artists/${artist.id}/enrich`, { method: 'POST' }))
			toasts.info(t('artist.refreshQueued'));
	}

	let syncing = $state(false);

	async function syncDiscography() {
		if (!artist) return;
		syncing = true;
		const data = await request<{ added: number; linked: number }>(
			`/api/music/artists/${artist.id}/discography/sync`,
			{ method: 'POST' }
		);
		syncing = false;
		if (!data) return;
		toasts.success(
			t('artist.discographySynced', { added: formatNumber(data.added), linked: formatNumber(data.linked) })
		);
		await load();
	}

	async function addToLibrary() {
		if (!artist) return;
		const said = await request<{ id: number; name?: string; releases?: number; already?: boolean }>(
			'/api/music/artists',
			json({ deezer_id: artist.deezer_id })
		);
		if (!said) return;
		if (said.already) toasts.info(t('artist.already', { name: artist.name }));
		else
			toasts.success(
				t('artist.added', { name: said.name ?? artist.name, releases: formatNumber(said.releases ?? 0) })
			);
		await load();
	}
</script>

{#if artist}
	<ArtistHeader
		{artist}
		{syncing}
		onpickportrait={() =>
			(picker = { entity: 'artists', id: artist!.id, title: t('artwork.artistTitle') })}
		onsync={syncDiscography}
		onrefresh={refreshMetadata}
	/>

	{#if artist.enrich_status === 'pending'}
		<Notice><p>{t('artist.enrichPending')}</p></Notice>
	{:else if artist.enrich_status === 'failed'}
		<Notice tone="err"><p>{t('artist.enrichFailed')}</p></Notice>
	{:else if artist.enrich_status === 'unresolved'}
		<IdentityPanel artistId={artist.id} />
	{/if}

	{#if !artist.monitored}
		<Notice tone="warn">
			<p class="adopt">
				{t('artist.notInLibrary')}
				{#if artist.deezer_id}
					<Button tone="accent" onclick={addToLibrary}>{t('artist.addToLibrary')}</Button>
				{/if}
			</p>
		</Notice>
	{/if}

	{#snippet tracklist(release: Release)}
		<TrackList
			releaseId={release.id}
			data={trackLists[release.id]}
			{editingRow}
			bind:editingTitle
			onretagall={retagAll}
			onfixtag={fixTag}
			onstartedit={startEditTitle}
			oncanceledit={() => (editingRow = null)}
			onsavetitle={saveEditedTitle}
			onaccepttag={(row) => patchTitle(row, row.tag_title!)}
			ondeletefile={deleteFile}
		/>
	{/snippet}

	{#if studioReleases.length > 0}
		<ReleaseTable
			releases={studioReleases}
			{expandedReleaseId}
			{trackLists}
			{tracklist}
			ontoggletracks={toggleTracks}
			onpickcover={(release) =>
				(picker = { entity: 'releases', id: release.id, title: release.title })}
			ondownload={download}
			ondeletefiles={deleteFiles}
			onpick={(release) => (candidatePicking = release)}
		/>
	{/if}

	{#if otherReleases.length > 0}
		<details class="other-releases">
			<summary>{t('artist.otherReleases')} ({formatNumber(otherReleases.length)})</summary>
			<ReleaseTable
				releases={otherReleases}
				{expandedReleaseId}
				{trackLists}
				{tracklist}
				ontoggletracks={toggleTracks}
				onpickcover={(release) =>
					(picker = { entity: 'releases', id: release.id, title: release.title })}
				ondownload={download}
				ondeletefiles={deleteFiles}
				onpick={(release) => (candidatePicking = release)}
			/>
		</details>
	{/if}

	{#if picker}
		<ArtworkPicker
			entity={picker.entity}
			entityId={picker.id}
			title={picker.title}
			onclose={() => (picker = null)}
			onchanged={load}
		/>
	{/if}

	{#if candidatePicking}
		<CandidatePicker
			releaseId={candidatePicking.id}
			title={candidatePicking.title}
			onclose={() => (candidatePicking = null)}
			ongrabbed={load}
		/>
	{/if}
{/if}

<style>
	.adopt {
		display: flex;
		align-items: center;
		gap: 1rem;
	}
	.other-releases {
		margin-top: 1.5rem;
	}
	.other-releases summary {
		cursor: pointer;
		color: var(--muted);
		font-weight: 600;
		padding-bottom: 0.4rem;
	}
	.other-releases :global(table) {
		opacity: 0.75;
	}
</style>
