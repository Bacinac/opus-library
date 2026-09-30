<script lang="ts">
	// The folders the scan could not place, and the only two ways out of that
	// state: try the match again now that the catalog may know more, or delete
	// the folder from disk. Both writes happen here; the page is told what to
	// re-read afterwards.

	import { t, type MessageKey } from '$lib/i18n';
	import { ArmedButton, Button, formatNumber, json, plural, request, toasts } from '$lib/kit';
	import FolderReport from './FolderReport.svelte';
	import type { UnmatchedFolder } from '$lib/music/types';

	let {
		folders,
		scanRunning,
		onartistschanged,
		onfolderschanged
	}: {
		folders: UnmatchedFolder[];
		scanRunning: boolean;
		onartistschanged: () => Promise<void>;
		onfolderschanged: () => Promise<void>;
	} = $props();

	let busyFolder = $state<string | null>(null);

	async function rematchFolder(entry: UnmatchedFolder) {
		busyFolder = entry.folder;
		const said = await request<{ outcome: string; matched?: number; total?: number }>(
			'/api/music/library/folders/rematch',
			json({ folder: entry.folder })
		);
		busyFolder = null;
		if (!said) return;
		const text = t(`libimport.outcome.${said.outcome}` as MessageKey, {
			matched: formatNumber(said.matched ?? 0),
			total: formatNumber(said.total ?? 0)
		});
		if (said.outcome === 'adopted' || said.outcome === 'partial') {
			toasts.success(`${entry.folder}: ${text}`);
			await onartistschanged();
		} else {
			toasts.info(`${entry.folder}: ${text}`);
		}
		await onfolderschanged();
	}

	async function deleteFolder(entry: UnmatchedFolder) {
		busyFolder = entry.folder;
		const deleted = await request(
			'/api/music/library/folders',
			json({ folder: entry.folder }, 'DELETE')
		);
		busyFolder = null;
		if (!deleted) return;
		toasts.success(t('music.library.folderDeleted', { folder: entry.folder }));
		await onfolderschanged();
	}
</script>

{#if folders.length > 0}
	<FolderReport summary={`${t('music.library.unmatched')} (${formatNumber(folders.length)})`}>
		{#each folders as entry (entry.folder)}
			<tr>
				<td class="folder"><code>{entry.folder}</code></td>
				<td>
					{#if entry.tag_artist}{entry.tag_artist}{#if entry.tag_album} — {entry.tag_album}{/if}{/if}
				</td>
				<td class="failed">
					{plural(entry.files, 'music.library.files.one', 'music.library.files.few', 'music.library.files.many')}
				</td>
				<td>
					<div class="actions">
					<Button
						size="small"
						disabled={busyFolder === entry.folder || scanRunning}
						onclick={() => rematchFolder(entry)}
					>
						{t('music.library.rematch')}
					</Button>
					<ArmedButton
						size="small"
						disabled={busyFolder === entry.folder}
						onconfirm={() => deleteFolder(entry)}
					>
						{t('music.library.deleteFolder')}
					</ArmedButton>
					</div>
				</td>
			</tr>
		{/each}
	</FolderReport>
{/if}

<style>
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.35rem;
	}
</style>
