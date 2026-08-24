import React from 'react';
import {Composition} from 'remotion';
import {PedazoDeManco} from './scene/PedazoDeManco';

/**
 * Render contract (full version in README.md):
 *   1920x1080 - exactly 10s - H.264 mp4
 *
 * The frame count is FPS * DURATION_SECONDS, so raising FPS to 60 keeps the
 * piece exactly ten seconds long and nothing else has to change. Every beat in
 * scene/timing.ts is expressed in SECONDS for the same reason.
 */
export const FPS = 30;
export const DURATION_SECONDS = 10;

export const RemotionRoot: React.FC = () => {
	return (
		<Composition
			id="PedazoDeManco"
			component={PedazoDeManco}
			durationInFrames={FPS * DURATION_SECONDS}
			fps={FPS}
			width={1920}
			height={1080}
		/>
	);
};
