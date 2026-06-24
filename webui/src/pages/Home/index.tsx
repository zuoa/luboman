import services from '@/services/luboman';
import { PageContainer } from '@ant-design/pro-components';
import { useModel } from '@umijs/max';
import {
  AppstoreOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import {
  Avatar,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Tooltip,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import defaultRoomImg from '../../assets/default.jpg';
import styles from './index.less';

const { listLiveRoom } = services.LiveRoom;

const HomePage: React.FC = () => {
  const { name } = useModel('global');
  const [list, setList] = useState<API.LiveRoomInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listLiveRoom();
      setList(Array.isArray(data) ? data : []);
    } catch (error) {
      // 统一错误层已弹 toast
      setList([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // 数据看板：纯前端从已有列表派生，无新增 API
  const stats = useMemo(() => {
    const live = list.filter((r) => r.live_state === 1).length;
    const recording = list.filter((r) => r.status === 'WORKING').length;
    return { live, recording, total: list.length };
  }, [list]);

  const statCards = [
    {
      key: 'live',
      title: '直播中',
      value: stats.live,
      icon: <ThunderboltOutlined />,
      color: 'var(--lb-error)',
    },
    {
      key: 'recording',
      title: '录制中',
      value: stats.recording,
      icon: <VideoCameraOutlined />,
      color: 'var(--lb-color-primary)',
    },
    {
      key: 'total',
      title: '直播间总数',
      value: stats.total,
      icon: <AppstoreOutlined />,
      color: 'var(--lb-text-secondary)',
    },
  ];

  if (loading) {
    return (
      <PageContainer ghost>
        <div style={{ textAlign: 'center', padding: '50px 0' }}>
          <Spin size="large" />
        </div>
      </PageContainer>
    );
  }

  return (
    <PageContainer ghost>
      <div className={styles.toolbar}>
        <span className={styles.title}>{name} · 直播间总览</span>
        <Button size="small" icon={<ReloadOutlined />} onClick={fetchData}>
          刷新
        </Button>
      </div>

      <Row gutter={[16, 16]} className={styles.statsRow}>
        {statCards.map((s) => (
          <Col key={s.key} xs={24} sm={8}>
            <Card bordered={false} className={styles.statCard}>
              <div className={styles.statRow}>
                <div className={styles.statIcon} style={{ color: s.color }}>
                  {s.icon}
                </div>
                <Statistic
                  title={s.title}
                  value={s.value}
                  valueStyle={{ fontWeight: 600 }}
                />
              </div>
            </Card>
          </Col>
        ))}
      </Row>

      {!list.length ? (
        <Empty description="暂无直播间数据" style={{ marginTop: 48 }} />
      ) : (
        <Row gutter={[16, 16]}>
          {list.map((room, index) => {
            const key = `room-${room.id || index}`;
            const coverUrl =
              room.room_cover_url &&
              `https://wsrv.nl/?url=${encodeURIComponent(room.room_cover_url)}&w=400&h=225&output=webp`;
            const avatarUrl =
              room.room_owner_avatar &&
              `https://wsrv.nl/?url=${encodeURIComponent(room.room_owner_avatar)}&w=100&h=100&output=webp`;
            return (
              <Col key={key} xs={24} sm={12} md={8} lg={6} xl={4}>
                <Tooltip
                  title={
                    <div>
                      <div>
                        <strong>{room.room_name}</strong>
                      </div>
                      <div>{room.room_title || '暂无标题'}</div>
                      <div
                        style={{
                          marginTop: 4,
                          fontSize: '12px',
                          opacity: 0.8,
                        }}
                      >
                        {room.live_state === 1
                          ? '正在直播'
                          : `最后直播: ${room.last_living_time || '未知时间'}`}
                      </div>
                    </div>
                  }
                  placement="top"
                >
                  <div className={styles.card}>
                    <div className={styles.imageContainer}>
                      <img
                        src={coverUrl || defaultRoomImg}
                        style={{
                          opacity: room.live_state === 1 ? 1 : 0.3,
                        }}
                        onError={(e) => {
                          e.currentTarget.onerror = null;
                          e.currentTarget.src = defaultRoomImg;
                        }}
                        alt={room.room_name}
                      />
                      {/* 直播状态标识 */}
                      <div className={styles.liveStatusBadge}>
                        <span
                          className={`${styles.liveStatus} ${
                            room.live_state === 1
                              ? styles.live
                              : styles.offline
                          }`}
                        >
                          {room.live_state === 1 ? '直播中' : '未开播'}
                        </span>
                      </div>
                      {/* 平台标识 */}
                      <div className={styles.platformBadge}>
                        <span className={styles.platformTag}>
                          {room.room_platform}
                        </span>
                      </div>
                    </div>

                    {/* 房间信息 */}
                    <div className={styles.roomInfo}>
                      <div className={styles.streamerInfo}>
                        <Avatar src={avatarUrl || defaultRoomImg} size={28} />
                        <div className={styles.roomDetails}>
                          <div className={styles.roomName}>
                            <a
                              href={room.room_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              title={room.room_name}
                            >
                              {room.room_name}
                            </a>
                          </div>
                          <div
                            className={styles.roomTitle}
                            title={room.room_title || ''}
                          >
                            {room.room_title || ''}
                          </div>
                          <div className={styles.lastLiveTime}>
                            {room.live_state === 1
                              ? '正在直播'
                              : room.last_living_time || ''}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </Tooltip>
              </Col>
            );
          })}
        </Row>
      )}
    </PageContainer>
  );
};

export default HomePage;
