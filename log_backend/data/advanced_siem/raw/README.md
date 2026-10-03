---
license: mit
language:
- en
tags:
- siem
- cybersecurity
pretty_name: sunny thakur
size_categories:
- 100K<n<1M
---
# Advanced SIEM Dataset

Dataset Description

The advanced_siem_dataset is a synthetic dataset of 100,000 security event records designed for training machine learning (ML) and artificial intelligence (AI) models in cybersecurity.

It simulates logs from Security Information and Event Management (SIEM) systems, capturing diverse event types such as firewall activities, intrusion detection system (IDS) alerts, authentication attempts, endpoint activities, network traffic, cloud operations, IoT device events, and AI system interactions.

The dataset includes advanced metadata, MITRE ATT&CK techniques, threat actor associations, and unconventional indicators of compromise (IOCs), making it suitable for tasks like anomaly detection, threat classification, predictive analytics, and user and entity behavior analytics (UEBA).
```java
Paper: N/A
Point of Contact: sunny thakur ,sunny48445@gmail.com
Size of Dataset: 100,000 records
File Format: JSON Lines (.jsonl)
License: MIT License
```
# Dataset Structure

The dataset is stored in a single train split in JSON Lines format, with each record representing a security event. Below is the schema:

```

Field
Type
Description



event_id
String
Unique identifier (UUID) for the event.


timestamp
String
ISO 8601 timestamp of the event.


event_type
String
Event category: firewall, ids_alert, auth, endpoint, network, cloud, iot, ai.


source
String
Security tool and version (e.g., "Splunk v9.0.2").


severity
String
Severity level: info, low, medium, high, critical, emergency.


description
String
Human-readable summary of the event.


raw_log
String
CEF-formatted raw log with optional noise.


advanced_metadata
Dict
Metadata including geo_location, device_hash, user_agent, session_id, risk_score, confidence.


behavioral_analytics
Dict
Optional; includes baseline_deviation, entropy, frequency_anomaly, sequence_anomaly (10% of records).


Event-specific fields
Varies
E.g., src_ip, dst_ip, alert_type (for ids_alert), user (for auth), action, etc.
```
```java
Sample Record:
{
  "event_id": "123e4567-e89b-12d3-a456-426614174000",
  "timestamp": "2025-07-11T11:27:00+00:00",
  "event_type": "ids_alert",
  "source": "Snort v2.9.20",
  "severity": "high",
  "description": "Snort Alert: Zero-Day Exploit detected from 192.168.1.100 targeting N/A | MITRE Technique: T1059.001",
  "raw_log": "CEF:0|Snort v2.9.20|SIEM|1.0|100|ids_alert|high| desc=Snort Alert: Zero-Day Exploit detected from 192.168.1.100 targeting N/A | MITRE Technique: T1059.001",
  "advanced_metadata": {
    "geo_location": "United States",
    "device_hash": "a1b2c3d4e5f6",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "session_id": "987fcdeb-1234-5678-abcd-426614174000",
    "risk_score": 85.5,
    "confidence": 0.95
  },
  "alert_type": "Zero-Day Exploit",
  "signature_id": "SIG-1234",
  "category": "Exploit",
  "additional_info": "MITRE Technique: T1059.001"
}
```
# Intended Use

This dataset is intended for:
```
Anomaly Detection: Identify unusual patterns (e.g., zero-day exploits, beaconing) using unsupervised learning.
Threat Classification: Classify events by severity or event_type for incident prioritization.
User and Entity Behavior Analytics (UEBA): Detect insider threats or compromised credentials by analyzing auth or endpoint events.
Predictive Analytics: Forecast high-risk periods using time-series analysis of risk_score and timestamp.
Threat Hunting: Leverage MITRE ATT&CK techniques and IOCs in additional_info for threat intelligence.
Red Teaming: Simulate adversarial scenarios (e.g., APTs, DNS tunneling) for testing SIEM systems.
```
# Loading the Dataset

Install the datasets library:

```python
pip install datasets

Load the dataset from Hugging Face Hub:
from datasets import load_dataset

dataset = load_dataset("your-username/advanced_siem_dataset", split="train")
print(dataset)

For large-scale processing, use streaming:
dataset = load_dataset("your-username/advanced_siem_dataset", streaming=True)
for example in dataset["train"]:
    print(example["event_type"], example["severity"])
    break

Preprocessing
Preprocessing is critical for ML/AI tasks. Below are recommended steps:

Numerical Features:

Extract risk_score and confidence from advanced_metadata.
Normalize using StandardScaler:from sklearn.preprocessing import StandardScaler
import pandas as pd

df = dataset.to_pandas()
df['risk_score'] = df['advanced_metadata'].apply(lambda x: x['risk_score'])
df['confidence'] = df['advanced_metadata'].apply(lambda x: x['confidence'])
scaler = StandardScaler()
X = scaler.fit_transform(df[['risk_score', 'confidence']])
```



# Categorical Features:
```java
Encode event_type, severity, and other categorical fields using LabelEncoder or one-hot encoding:from sklearn.preprocessing import LabelEncoder

le = LabelEncoder()
df['event_type_encoded'] = le.fit_transform(df['event_type'])




Text Features:

Tokenize description or raw_log for NLP tasks using Hugging Face Transformers:from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
def preprocess_function(examples):
    return tokenizer(examples["description"], padding="max_length", truncation=True)
processed_dataset = dataset.map(preprocess_function, batched=True)




Handling Missing Values:

Replace "N/A" in dst_ip (for some ids_alert events) with a placeholder (e.g., 0.0.0.0) or filter out.


Feature Engineering:

Extract MITRE ATT&CK techniques and IOCs from additional_info using regex or string parsing.
Convert timestamp to datetime for temporal analysis:df['timestamp'] = pd.to_datetime(df['timestamp'])


```


# Training Examples
Below are example ML/AI workflows using Hugging Face and other libraries.
Anomaly Detection (Isolation Forest)
Detect unusual events like zero-day exploits or beaconing:
```python
from datasets import load_dataset
from sklearn.ensemble import IsolationForest
import pandas as pd

dataset = load_dataset("darkknight25/advanced_siem_dataset", split="train")
df = dataset.to_pandas()
X = df['advanced_metadata'].apply(lambda x: [x['risk_score'], x['confidence']]).tolist()

model = IsolationForest(contamination=0.05, random_state=42)
df['anomaly'] = model.predict(X)
anomalies = df[df['anomaly'] == -1]
print(f"Detected {len(anomalies)} anomalies")
```
Threat Classification (Transformers)
Classify events by severity using a BERT model:
```python
from datasets import load_dataset
from transformers import AutoModelForSequenceClassification, Trainer, TrainingArguments
from sklearn.preprocessing import LabelEncoder

dataset = load_dataset("your-username/advanced_siem_dataset", split="train")
le = LabelEncoder()
dataset = dataset.map(lambda x: {"labels": le.fit_transform([x["severity"]])[0]})
dataset = dataset.map(lambda x: tokenizer(x["description"], padding="max_length", truncation=True))

train_test = dataset.train_test_split(test_size=0.2)
train_dataset = train_test["train"]
eval_dataset = train_test["test"]

model = AutoModelForSequenceClassification.from_pretrained("bert-base-uncased", num_labels=len(le.classes_))
training_args = TrainingArguments(
    output_dir="./results",
    evaluation_strategy="epoch",
    per_device_train_batch_size=16,
    per_device_eval_batch_size=16,
    num_train_epochs=3,
)
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
)
trainer.train()
```
Time-Series Forecasting (Prophet)
Forecast risk_score trends:
```java
from datasets import load_dataset
from prophet import Prophet
import pandas as pd

dataset = load_dataset("darkknight25/advanced_siem_dataset", split="train")
df = dataset.to_pandas()
ts_data = df[['timestamp', 'advanced_metadata']].copy()
ts_data['ds'] = pd.to_datetime(ts_data['timestamp'])
ts_data['y'] = ts_data['advanced_metadata'].apply(lambda x: x['risk_score'])

model = Prophet()
model.fit(ts_data[['ds', 'y']])
future = model.make_future_dataframe(periods=30)
forecast = model.predict(future)
print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail())
```


```python
UEBA (K-Means Clustering)
Cluster auth events to detect insider threats:
from datasets import load_dataset
from sklearn.cluster import KMeans
import pandas as pd

dataset = load_dataset("darkknight25/advanced_siem_dataset", split="train")
auth_df = dataset.filter(lambda x: x["event_type"] == "auth").to_pandas()
X = auth_df['advanced_metadata'].apply(lambda x: [x['risk_score'], x['confidence']]).tolist()

kmeans = KMeans(n_clusters=3, random_state=42)
auth_df['cluster'] = kmeans.fit_predict(X)
print(auth_df.groupby('cluster')[['user', 'action']].describe())
```
# Limitations
```java
Synthetic Nature: The dataset is synthetic and may not fully capture real-world SIEM log complexities, such as vendor-specific formats or noise patterns.
Class Imbalance: Certain event_type (e.g., ai, iot) or severity (e.g., emergency) values may be underrepresented. Use data augmentation or reweighting for balanced training.
Missing Values: Some dst_ip fields in ids_alert events are "N/A", requiring imputation or filtering.
Timestamp Anomalies: 5% of records include intentional timestamp anomalies (future/past dates) to simulate time-based attacks, which may require special handling.
```
# Bias and Ethical Considerations
```
The dataset includes synthetic geo_location data with a 5% chance of high-risk locations (e.g., North Korea, Russia). This is for anomaly simulation and not indicative of real-world biases. Ensure models do not inadvertently profile based on geo_location.
User names and other PII-like fields are generated using faker and do not represent real individuals.
Models trained on this dataset should be validated to avoid overfitting to synthetic patterns.
```
# Citation
```
If you use this dataset in your work, please cite:
@dataset{advanced_siem_dataset_2025,
  author = {sunnythakur},
  title = {Advanced SIEM Dataset for Cybersecurity ML},
  year = {2025},
  publisher = {Hugging Face},
  url = {https://huggingface.co/datasets/darkknight25/advanced_siem_dataset}
}
```

# Acknowledgments
```
Generated using a custom Python script (datasetcreator.py) with faker and numpy.
Inspired by real-world SIEM log formats (e.g., CEF) and MITRE ATT&CK framework.
Thanks to the Hugging Face community for providing tools to share and process datasets.
```
