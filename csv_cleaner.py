import pandas as pd
import numpy as np
import os


class CSVCleanerSkill:
    def __init__(self, file_path):
        self.file_path = file_path
        self.df = None
        self.report = []

    def run(self):
        try:
            # 1. 读取数据：优先尝试 utf-8，若遇乱码则自动回退到 gbk 编码
            try:
                self.df = pd.read_csv(self.file_path, encoding='utf-8')
            except UnicodeDecodeError:
                self.df = pd.read_csv(self.file_path, encoding='gbk')

            self.report.append(f"✅ 成功加载文件，共 {self.df.shape[0]} 行，{self.df.shape[1]} 列。")

            # 2. 清洗重复行：检测并直接删除完全重复的数据录入
            duplicates = self.df.duplicated().sum()
            if duplicates > 0:
                self.df = self.df.drop_duplicates()
                self.report.append(f"⚠️ 发现并清除了 {duplicates} 行重复数据。")

            # 3. 填补缺失值：数值型(is_numeric_dtype)用中位数（median)填充，文本/分类型用众数(mode)填充
            missing_data = self.df.isnull().sum()
            missing_cols = missing_data[missing_data > 0]
            if not missing_cols.empty:
                for col in missing_cols.index:#若有破洞
                    if pd.api.types.is_numeric_dtype(self.df[col]):
                        self.df[col] = self.df[col].fillna(self.df[col].median())
                    else:
                        self.df[col] = self.df[col].fillna(self.df[col].mode()[0])
                self.report.append(f"⚠️ 检测到缺失值，已自动对 {len(missing_cols)} 个列完成了智能填充。")

            # 4. 抹平异常值：利用 Z-score (标准差) 算法锁定极端值，并替换为平均水平
            outlier_count = 0
            for col in self.df.select_dtypes(include=[np.number]).columns:
                mean = self.df[col].mean()# 算平均数
                std = self.df[col].std()# 算标准差
                if std > 0:
                    z_scores = (self.df[col] - mean) / std # 标准化公式
                    # 绝对值大于 3 视为异常刺头
                    outliers = self.df[np.abs(z_scores) > 3]
                    if not outliers.empty:
                        outlier_count += len(outliers)
                        # 将这些异常刺头强制替换为该列的均值
                        self.df.loc[np.abs(z_scores) > 3, col] = mean

            if outlier_count > 0:
                self.report.append(f"⚠️ 拦截到 {outlier_count} 个严重偏离的异常值，已将其平滑至均值。")

            # 5. 封装与导出：将清洗完的纯净数据打包为 Excel 文件
            output_path = self.file_path.rsplit('.', 1)[0] + '_cleaned.xlsx'
            self.df.to_excel(output_path, index=False)
            self.report.append(f"🎉 任务大功告成！清洗后的 Excel 文件已生成：{output_path}")

            # 将整个处理过程的报告合并为一段文本返回
            return "\n".join(self.report)

        except Exception as e:
            return f"❌ 抱歉，处理失败，遇到了预料之外的错误: {str(e)}"


# 测试入口：如果你想直接运行这个脚本看看效果，可以把下面几行的注释（#）去掉
if __name__ == "__main__":
    #请确保同目录下有一个名为 test.csv 的文件用于测试
    cleaner = CSVCleanerSkill("test.csv")
    print(cleaner.run())
    pass