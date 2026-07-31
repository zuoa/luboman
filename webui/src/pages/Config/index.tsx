import services from '@/services/luboman';
import {
  InfoCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import {
  PageContainer,
  ProCard,
  ProForm,
  ProFormDigit,
  ProFormSelect,
  ProFormText,
} from '@ant-design/pro-components';
import { Button, Space, message } from 'antd';
import { useState } from 'react';
import styles from './index.less';

const ConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const { getConfig, setConfig } = services.Config;

  const initialConfigValues = {
    custom_filename: '{room_name}.%Y_%m_%d_%H_%M_%S.{title}',
    live_offline_judge_delay: 300,
    notify_platform: 'bark',
    notify_token: '',
    segment_duration: '01:00:00',
    filtering_threshold_file_size: 10,
    local_video_file_remain_days: 3,
    record_file_stale_timeout_seconds: 3600,
    dance_clip_sample_interval: 2,
    dance_clip_merge_gap_seconds: 30,
    dance_clip_min_clip_seconds: 60,
    dance_clip_pad_seconds: 2,
    dance_clip_accurate_cut: 'false',
    dance_clip_concurrency: 1,
    dance_clip_boundary_gap_seconds: 10,
    dance_clip_title_template:
      '【{room_name}】%Y年%m月%d日 %H时 舞蹈片段{seq}',
    douyin_cookies: '',
    afreecatv_username: '',
    afreecatv_password: '',
  };

  const handleSubmit = async (values: any) => {
    setLoading(true);
    try {
      await setConfig(values);
      message.success('配置保存成功！');
    } catch (error) {
      message.error('配置保存失败，请重试');
    } finally {
      setLoading(false);
    }
  };

  // 分区标题：主色左边框 + 名称 + 灰色描述，令牌驱动
  const sectionTitle = (name: string, desc?: string) => (
    <div className={styles.sectionTitle}>
      <span className={styles.sectionName}>{name}</span>
      {desc && <span className={styles.sectionDesc}>{desc}</span>}
    </div>
  );

  return (
    <PageContainer
      ghost
      header={{
        title: '系统配置',
        subTitle: '配置直播录制和系统参数',
      }}
    >
      <ProForm
        submitter={{
          render: (props) => (
            <div className={styles.formActions}>
              <Space>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => props.form?.resetFields()}
                >
                  重置
                </Button>
                <Button
                  type="primary"
                  icon={<SaveOutlined />}
                  loading={loading}
                  onClick={() => props.form?.submit?.()}
                >
                  保存配置
                </Button>
              </Space>
            </div>
          ),
        }}
        request={async () => {
          try {
            const data = await getConfig();
            return { data: { ...initialConfigValues, ...(data || {}) } };
          } catch {
            return { data: initialConfigValues };
          }
        }}
        onFinish={handleSubmit}
      >
        <ProCard
          title={sectionTitle('基本设置', '配置录制文件和基本参数')}
          bordered
          headerBordered
          style={{ marginBottom: 16 }}
        >
          <div className={styles.tip}>
            <InfoCircleOutlined />
            <span>
              文件名格式支持变量：{'{room_name}'} - 房间名，{'{title}'} -
              直播标题， %Y_%m_%d_%H_%M_%S - 时间格式
            </span>
          </div>
          <div className={styles.fieldGroup}>
            <ProFormText
              name="custom_filename"
              label="录制文件名格式"
              placeholder="{room_name}.%Y_%m_%d_%H_%M_%S.{title}"
              extra="支持时间变量和房间信息变量"
              rules={[{ required: true, message: '请输入文件名格式' }]}
            />
            <ProFormDigit
              name="live_offline_judge_delay"
              label="下播延迟监测（秒）"
              placeholder="300"
              min={60}
              max={1800}
              extra="防止网络波动导致的误判"
            />
            <ProFormDigit
              name="local_video_file_remain_days"
              label="本地文件保留天数"
              placeholder="3"
              min={1}
              max={365}
              extra="超过指定天数的文件将被自动清理"
            />
            <ProFormDigit
              name="record_file_stale_timeout_seconds"
              label="录制状态超时（秒）"
              placeholder="3600"
              min={60}
              max={86400}
              extra="录制中文件超过该时间无写入活动时自动标记为已完成"
            />
          </div>
        </ProCard>

        <ProCard
          title={sectionTitle('消息推送', '配置开播、下播等事件的推送通知')}
          bordered
          headerBordered
          style={{ marginBottom: 16 }}
        >
          <div className={styles.fieldGroup}>
            <ProFormSelect
              name="notify_platform"
              label="推送平台"
              valueEnum={{
                bark: 'Bark (iOS)',
                pushplus: 'PushPlus',
                tg: 'Telegram',
              }}
              placeholder="选择推送平台"
              extra="选择你使用的推送服务平台"
            />
            <ProFormText
              name="notify_token"
              label="推送Token"
              placeholder="请输入推送服务的Token"
              extra="从对应平台获取的推送凭证"
            />
          </div>
        </ProCard>

        <ProCard
          title={sectionTitle('视频分段', '配置录制文件的分段策略')}
          bordered
          headerBordered
          style={{ marginBottom: 16 }}
        >
          <div className={styles.fieldGroup}>
            <ProFormText
              name="segment_duration"
              label="分段时长"
              placeholder="01:00:00"
              extra="格式：HH:MM:SS，设置每段视频的时长"
            />
            <ProFormDigit
              name="segment_file_size"
              label="分段文件大小（MB）"
              placeholder="1024"
              min={100}
              max={10240}
              extra="按文件大小分段，优先级低于时长分段"
            />
            <ProFormDigit
              name="filtering_threshold_file_size"
              label="忽略文件大小（MB）"
              placeholder="10"
              min={1}
              max={1024}
              extra="小于此大小的文件将被忽略"
            />
          </div>
        </ProCard>

        <ProCard
          title={sectionTitle(
            '切片探测',
            '配置三分屏（舞蹈）画面探测与自动切片参数',
          )}
          bordered
          headerBordered
          style={{ marginBottom: 16 }}
        >
          <div className={styles.tip}>
            <InfoCircleOutlined />
            <span>
              在文件管理页勾选录像后点「探测」，系统会检测三分屏画面区间并自动切片，
              切片会出现在文件管理列表中
            </span>
          </div>
          <div className={styles.fieldGroup}>
            <ProFormDigit
              name="dance_clip_sample_interval"
              label="采样间隔（秒）"
              placeholder="2"
              min={0.5}
              max={10}
              extra="每隔多少秒取一帧判断是否为三分屏，越小越精确但越慢"
            />
            <ProFormDigit
              name="dance_clip_merge_gap_seconds"
              label="区间合并间隔（秒）"
              placeholder="30"
              min={0}
              max={300}
              extra="相邻三分屏区间间隔小于该值时合并为一段"
            />
            <ProFormDigit
              name="dance_clip_min_clip_seconds"
              label="最短切片时长（秒）"
              placeholder="60"
              min={5}
              max={3600}
              extra="合并后短于该时长的区间将被丢弃"
            />
            <ProFormDigit
              name="dance_clip_pad_seconds"
              label="头尾扩展（秒）"
              placeholder="2"
              min={0}
              max={30}
              extra="切片区间头尾各扩展的秒数，避免切掉开头结尾"
            />
            <ProFormDigit
              name="dance_clip_boundary_gap_seconds"
              label="跨分段拼接间隔（秒）"
              placeholder="10"
              min={0}
              max={120}
              extra="自动切片时，相邻两个录制分段的时间间隔小于该值且首尾都是舞蹈画面时，拼接为一个切片"
            />
            <ProFormSelect
              name="dance_clip_accurate_cut"
              label="切割模式"
              options={[
                { label: '快速无损（对齐关键帧，可能有数秒偏差）', value: 'false' },
                { label: '精确切割（重编码，慢但帧级精确）', value: 'true' },
              ]}
              extra="快速模式直接复制流不重编码；精确模式统一输出 mp4"
            />
            <ProFormDigit
              name="dance_clip_concurrency"
              label="任务并发数"
              placeholder="1"
              min={1}
              max={4}
              extra="同时执行的切片任务数，探测吃 CPU，不宜过大；重启后生效"
            />
            <ProFormText
              name="dance_clip_title_template"
              label="投稿标题模板"
              placeholder="【{room_name}】%Y年%m月%d日 %H时 舞蹈片段{seq}"
              extra="支持 {room_name} 主播名称、{room_title} 直播标题、{seq} 切片序号，以及 %Y年%m月%d日 %H时 等时间格式（取切片开始时间）"
            />
          </div>
        </ProCard>

        <ProCard
          title={sectionTitle('平台设置', '配置各直播平台的特殊参数')}
          bordered
          headerBordered
          style={{ marginBottom: 16 }}
        >
          <div className={styles.tip}>
            <InfoCircleOutlined />
            <span>
              某些平台需要登录凭证才能获取高质量直播流，请根据需要填写相关信息
            </span>
          </div>
          <div className={styles.fieldGroup}>
            <ProFormText
              name="douyin_cookies"
              label="抖音Cookie"
              placeholder="从浏览器复制完整的Cookie字符串"
              extra="用于获取抖音高质量直播流"
            />
            <ProFormText
              name="afreecatv_username"
              label="AfreecaTV用户名"
              placeholder="AfreecaTV账号用户名"
              extra="AfreecaTV平台登录用户名"
            />
            <ProFormText.Password
              name="afreecatv_password"
              label="AfreecaTV密码"
              placeholder="AfreecaTV账号密码"
              extra="AfreecaTV平台登录密码"
            />
          </div>
        </ProCard>
      </ProForm>
    </PageContainer>
  );
};

export default ConfigPage;
