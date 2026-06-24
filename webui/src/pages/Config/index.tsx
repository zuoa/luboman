import {
  PageContainer,
  ProCard,
  ProForm,
  ProFormDigit,
  ProFormSelect,
  ProFormText,
} from '@ant-design/pro-components';
import { Button, Space, message } from 'antd';
import {
  InfoCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { useState } from 'react';
import services from '@/services/luboman';
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
              文件名格式支持变量：{'{room_name}'} - 房间名，{'{title}'} - 直播标题，
              %Y_%m_%d_%H_%M_%S - 时间格式
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
