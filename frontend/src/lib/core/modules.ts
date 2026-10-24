import { moduleName, modulesFor } from '#lib/opus/index.js';

export const MODULE = moduleName('library');

export const MODULES = modulesFor('library', {
	downloads: import.meta.env.VITE_OPUS_DOWNLOADS_URL,
	player: import.meta.env.VITE_OPUS_PLAYER_URL
});
