import { Alert, Spin } from 'antd';
import flvjs from 'flv.js';
import React, { useEffect, useRef, useState } from 'react';
import styles from './index.less';

interface FlvPlayerProps {
  url: string;
}

/**
 * flv.js 驱动的 FLV 播放器。浏览器原生 <video> 不支持 FLV，
 * 这里用 flv.js 解封装 FLV 再喂给 MSE，由 <video> 渲染。
 * 非 FLV（mp4 等）请直接用原生 <video>，无需走这里。
 */
const FlvPlayer: React.FC<FlvPlayerProps> = ({ url }) => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const playerRef = useRef<flvjs.Player | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !url) return;

    setLoading(true);
    setError(undefined);

    if (!flvjs.isSupported()) {
      setError('当前浏览器不支持 MSE，无法播放 FLV，请改用现代桌面浏览器。');
      setLoading(false);
      return;
    }

    const player = flvjs.createPlayer({
      type: 'flv',
      url,
      isLive: false,
    });
    player.on(flvjs.Events.ERROR, (errorType, errorDetail) => {
      setError(`播放失败（${errorType} / ${errorDetail}），请确认文件可访问或后端已开启 Range。`);
      setLoading(false);
    });
    player.attachMediaElement(video);
    player.load();
    playerRef.current = player;

    // 弹窗由用户点击触发，尝试自动播放；被浏览器拦截时由用户点控件播放即可。
    const playPromise = player.play();
    if (playPromise && typeof playPromise.catch === 'function') {
      playPromise.catch(() => {});
    }

    return () => {
      try {
        player.pause();
        player.unload();
        player.detachMediaElement();
        player.destroy();
      } catch {
        // 卸载阶段忽略 teardown 异常
      }
      playerRef.current = null;
    };
  }, [url]);

  return (
    <div className={styles.flvWrap}>
      <video
        ref={videoRef}
        className={styles.player}
        controls
        playsInline
        onCanPlay={() => setLoading(false)}
      />
      {loading && !error ? (
        <div className={styles.flvOverlay}>
          <Spin />
        </div>
      ) : null}
      {error ? (
        <div className={styles.flvOverlay}>
          <Alert type="error" showIcon message={error} />
        </div>
      ) : null}
    </div>
  );
};

export default FlvPlayer;
